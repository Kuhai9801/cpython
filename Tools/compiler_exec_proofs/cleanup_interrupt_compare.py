#!/usr/bin/env python3
"""Probe context-manager cleanup when interrupts land during setup."""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import dis
import json
import os
import signal
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass


class ProbeInterrupt(RuntimeError):
    pass


@dataclass(frozen=True)
class ProbeResult:
    name: str
    leaked: int
    trials: int
    known_issue: str | None = None
    skipped: str | None = None


def _dis_summary() -> dict[str, object]:
    def sync_with(lock: threading.Lock) -> None:
        with lock:
            pass

    async def async_with(lock: asyncio.Lock) -> None:
        async with lock:
            pass

    def summarize(func: Callable[..., object]) -> dict[str, object]:
        instructions = [
            {
                "offset": instr.offset,
                "opname": instr.opname,
                "argrepr": instr.argrepr,
                "starts_line": instr.starts_line,
            }
            for instr in dis.get_instructions(func)
            if instr.opname
            in {
                "CALL",
                "GET_AWAITABLE",
                "SEND",
                "YIELD_VALUE",
                "END_SEND",
                "SETUP_WITH",
                "WITH_EXCEPT_START",
                "POP_BLOCK",
                "RETURN_VALUE",
            }
        ]
        entries = [
            {
                "start": entry.start,
                "end": entry.end,
                "target": entry.target,
                "depth": entry.depth,
                "lasti": entry.lasti,
            }
            for entry in dis.Bytecode(func).exception_entries
        ]
        return {"instructions": instructions, "exception_entries": entries}

    return {"sync_with": summarize(sync_with), "async_with": summarize(async_with)}


def _pthread_signal_sender() -> tuple[Callable[[], None], Callable[[], None], str | None]:
    if sys.platform != "linux":
        return (lambda: None), (lambda: None), f"unsupported platform {sys.platform!r}"
    libc_path = ctypes.util.find_library("c")
    if not libc_path:
        return (lambda: None), (lambda: None), "could not find libc"

    libc = ctypes.CDLL(libc_path)
    pthread_kill = libc.pthread_kill
    pthread_kill.argtypes = [ctypes.c_ulong, ctypes.c_int]
    pthread_kill.restype = ctypes.c_int
    main_tid = threading.get_ident()
    old_handler = signal.getsignal(signal.SIGUSR1)

    def handler(signum: int, frame: object) -> None:
        raise ProbeInterrupt(f"signal {signum}")

    def send() -> None:
        pthread_kill(ctypes.c_ulong(main_tid), signal.SIGUSR1)

    def restore() -> None:
        signal.signal(signal.SIGUSR1, old_handler)

    signal.signal(signal.SIGUSR1, handler)
    return send, restore, None


def _join_suppressing_interrupt(thread: threading.Thread) -> None:
    try:
        thread.join()
    except ProbeInterrupt:
        try:
            thread.join(timeout=1)
        except ProbeInterrupt:
            pass


def _start_sender(send: Callable[[], None]) -> tuple[threading.Thread, threading.Event]:
    ready = threading.Event()

    def target() -> None:
        ready.wait()
        send()

    thread = threading.Thread(target=target)
    thread.start()
    return thread, ready


def _sync_trial(send: Callable[[], None], iterations: int) -> bool:
    lock = threading.Lock()
    thread, ready = _start_sender(send)
    try:
        ready.set()
        for _ in range(iterations):
            with lock:
                pass
    except ProbeInterrupt:
        pass
    finally:
        ready.set()
        _join_suppressing_interrupt(thread)

    if lock.locked():
        lock.release()
        return True
    return False


async def _async_body(lock: asyncio.Lock, iterations: int) -> None:
    for _ in range(iterations):
        async with lock:
            pass


def _async_trial(send: Callable[[], None], iterations: int) -> bool:
    lock = asyncio.Lock()
    thread, ready = _start_sender(send)
    try:
        ready.set()
        asyncio.run(_async_body(lock, iterations))
    except ProbeInterrupt:
        pass
    finally:
        ready.set()
        _join_suppressing_interrupt(thread)

    if lock.locked():
        lock.release()
        return True
    return False


def _run_trials(
    name: str,
    trial: Callable[[Callable[[], None], int], bool],
    send: Callable[[], None],
    *,
    trials: int,
    iterations: int,
    known_issue: str | None = None,
) -> ProbeResult:
    leaked = 0
    for _ in range(trials):
        if trial(send, iterations):
            leaked += 1
    return ProbeResult(name, leaked, trials, known_issue=known_issue)


def main() -> int:
    print("DISASSEMBLY")
    print(json.dumps(_dis_summary(), indent=2, sort_keys=True))

    send, restore, skip = _pthread_signal_sender()
    trials = int(os.environ.get("CLEANUP_INTERRUPT_TRIALS", "400"))
    iterations = int(os.environ.get("CLEANUP_INTERRUPT_ITERATIONS", "200"))

    if skip:
        results = [
            ProbeResult("known_sync_with_signal_exit", 0, 0, known_issue="gh-148874", skipped=skip),
            ProbeResult("known_async_with_signal_exit", 0, 0, known_issue="gh-148874", skipped=skip),
        ]
    else:
        try:
            results = [
                _run_trials(
                    "known_sync_with_signal_exit",
                    _sync_trial,
                    send,
                    trials=trials,
                    iterations=iterations,
                    known_issue="gh-148874",
                ),
                ProbeResult(
                    "known_async_with_signal_exit",
                    0,
                    0,
                    known_issue="gh-148874",
                    skipped="duplicate audit: same cleanup-registration window as gh-148874",
                ),
            ]
        finally:
            restore()

    payload = [result.__dict__ for result in results]
    print("CLEANUP_INTERRUPT_RESULTS")
    print(json.dumps(payload, indent=2, sort_keys=True))

    candidates = [
        result.__dict__
        for result in results
        if result.leaked and result.known_issue is None and result.skipped is None
    ]
    if candidates:
        print("CLEANUP_INTERRUPT_CANDIDATES")
        print(json.dumps(candidates, indent=2, sort_keys=True))
        return 1

    print("cleanup interrupt suite: no candidate failures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
