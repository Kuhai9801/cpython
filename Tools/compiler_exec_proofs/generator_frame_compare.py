#!/usr/bin/env python3
"""Compare generator-frame mutation cases across execution modes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass


WARMUP = 1200


@dataclass(frozen=True)
class Case:
    name: str
    source: str


def _body(source: str) -> str:
    return textwrap.dedent(source).strip() + "\n"


CASES: list[Case] = [
    Case(
        "for_iter_gen_uses_generator_creation_code_after_function_rebind",
        _body(
            f"""
            def g():
                yield 1
                yield 2

            def h():
                yield 40
                yield 50

            OLD_CODE = g.__code__
            NEW_CODE = h.__code__

            def drive(limit):
                total = 0
                for _ in range(limit):
                    g.__code__ = OLD_CODE
                    gen = g()
                    g.__code__ = NEW_CODE
                    for value in gen:
                        total += value
                return total

            print(json.dumps({{"result": drive({WARMUP})}}))
            """
        ),
    ),
    Case(
        "send_gen_uses_generator_creation_code_after_function_rebind",
        _body(
            f"""
            def g():
                yield 3
                yield 4

            def h():
                yield 60
                yield 70

            OLD_CODE = g.__code__
            NEW_CODE = h.__code__

            def wrapper():
                g.__code__ = OLD_CODE
                gen = g()
                g.__code__ = NEW_CODE
                yield from gen

            def drive(limit):
                total = 0
                for _ in range(limit):
                    for value in wrapper():
                        total += value
                return total

            print(json.dumps({{"result": drive({WARMUP})}}))
            """
        ),
    ),
    Case(
        "generator_defaults_are_captured_at_creation_after_function_rebind",
        _body(
            f"""
            def g(value=5):
                yield value

            OLD_DEFAULTS = g.__defaults__

            def drive(limit):
                total = 0
                for _ in range(limit):
                    g.__defaults__ = OLD_DEFAULTS
                    gen = g()
                    g.__defaults__ = (80,)
                    for value in gen:
                        total += value
                return total

            print(json.dumps({{"result": drive({WARMUP})}}))
            """
        ),
    ),
]


def _script_for(case: Case) -> str:
    return "import json\n" + case.source


def _run_case(case: Case, env_updates: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(env_updates)
    env.setdefault("PYTHONHASHSEED", "0")
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tmp:
        tmp.write(_script_for(case))
        path = tmp.name
    try:
        return subprocess.run(
            [sys.executable, path],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _normalize(proc: subprocess.CompletedProcess[str]) -> dict[str, object]:
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip().splitlines(),
        "stderr_tail": proc.stderr.strip().splitlines()[-8:],
    }


def main() -> int:
    case_filter = os.environ.get("CASE_FILTER", "")
    modes = {
        "tier1": {"PYTHON_JIT": "0"},
        "tier2": {"PYTHON_JIT": "1"},
        "tier2_no_opt": {"PYTHON_JIT": "1", "PYTHON_UOPS_OPTIMIZE": "0"},
    }
    selected = [case for case in CASES if case_filter in case.name]
    if not selected:
        raise SystemExit(f"no cases selected by filter {case_filter!r}")

    mismatches: list[dict[str, object]] = []
    for case in selected:
        results = {mode: _normalize(_run_case(case, env)) for mode, env in modes.items()}
        print(f"CASE {case.name}")
        print(json.dumps(results, sort_keys=True))
        baseline = results["tier1"]
        for mode, result in results.items():
            if mode != "tier1" and result != baseline:
                mismatches.append(
                    {"case": case.name, "mode": mode, "baseline": baseline, "result": result}
                )

    if mismatches:
        print("MISMATCHES")
        print(json.dumps(mismatches, indent=2, sort_keys=True))
        return 1
    print("generator frame suite: no mismatches")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
