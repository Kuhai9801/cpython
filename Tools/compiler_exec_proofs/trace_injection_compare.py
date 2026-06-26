#!/usr/bin/env python3
"""Check whether trace-raised exceptions are caught by source-level handlers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass


@dataclass(frozen=True)
class TraceCase:
    name: str
    source: str
    known_issue: str | None = None


def _body(source: str) -> str:
    return textwrap.dedent(source).strip() + "\n"


CASES = [
    TraceCase(
        "assignment_in_try_except",
        _body(
            """
            def case():
                try:
                    value = 1  # TRACE_TARGET
                except Probe:
                    return "caught"
                return "missed"
            """
        ),
    ),
    TraceCase(
        "pass_in_try_except",
        _body(
            """
            def case():
                try:
                    pass  # TRACE_TARGET
                except Probe:
                    return "caught"
                return "missed"
            """
        ),
        known_issue="gh-148278",
    ),
    TraceCase(
        "known_148278_literal_in_try_except",
        _body(
            """
            def case():
                try:
                    42  # TRACE_TARGET
                except Probe:
                    return "caught"
                return "missed"
            """
        ),
        known_issue="gh-148278",
    ),
    TraceCase(
        "known_148278_continue_in_try_except",
        _body(
            """
            def case():
                for _ in range(1):
                    try:
                        continue  # TRACE_TARGET
                    except Probe:
                        return "caught"
                return "missed"
            """
        ),
        known_issue="gh-148278",
    ),
    TraceCase(
        "break_in_try_except",
        _body(
            """
            def case():
                for _ in range(1):
                    try:
                        break  # TRACE_TARGET
                    except Probe:
                        return "caught"
                return "missed"
            """
        ),
        known_issue="gh-148278",
    ),
    TraceCase(
        "return_in_try_except",
        _body(
            """
            def case():
                try:
                    return "missed"  # TRACE_TARGET
                except Probe:
                    return "caught"
            """
        ),
        known_issue="gh-148278",
    ),
    TraceCase(
        "if_header_in_try_except",
        _body(
            """
            def case():
                try:
                    if True:  # TRACE_TARGET
                        value = 1
                except Probe:
                    return "caught"
                return "missed"
            """
        ),
    ),
    TraceCase(
        "for_header_in_try_except",
        _body(
            """
            def case():
                try:
                    for item in (1,):  # TRACE_TARGET
                        value = item
                except Probe:
                    return "caught"
                return "missed"
            """
        ),
    ),
    TraceCase(
        "with_header_in_try_except",
        _body(
            """
            class CM:
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

            def case():
                try:
                    with CM():  # TRACE_TARGET
                        value = 1
                except Probe:
                    return "caught"
                return "missed"
            """
        ),
    ),
    TraceCase(
        "match_header_in_try_except",
        _body(
            """
            def case():
                try:
                    match {"kind": "x"}:  # TRACE_TARGET
                        case {"kind": "x"}:
                            value = 1
                except Probe:
                    return "caught"
                return "missed"
            """
        ),
    ),
    TraceCase(
        "except_handler_line_in_outer_try",
        _body(
            """
            def case():
                try:
                    try:
                        raise ValueError("sentinel")
                    except ValueError:  # TRACE_TARGET
                        value = 1
                except Probe:
                    return "caught"
                return "missed"
            """
        ),
    ),
    TraceCase(
        "finally_line_in_outer_try",
        _body(
            """
            def case():
                try:
                    try:
                        value = 1
                    finally:
                        value = 2  # TRACE_TARGET
                except Probe:
                    return "caught"
                return "missed"
            """
        ),
    ),
]


SCRIPT_TEMPLATE = """
import json
import sys


class Probe(Exception):
    pass


TARGET_LINES = __TARGET_LINES__
seen = set()


def tracer(frame, event, arg):
    if event == "line" and frame.f_code.co_filename == __file__ and frame.f_lineno in TARGET_LINES:
        if frame.f_lineno not in seen:
            seen.add(frame.f_lineno)
            raise Probe("trace injected")
    return tracer


__CASE_SOURCE__


sys.settrace(tracer)
try:
    try:
        result = case()
    except Probe:
        result = "uncaught"
finally:
    sys.settrace(None)

print(json.dumps({"result": result, "targets_seen": sorted(seen)}, sort_keys=True))
"""


def _script_for(case: TraceCase) -> str:
    script = SCRIPT_TEMPLATE.replace("__CASE_SOURCE__", case.source.rstrip())
    lines = script.splitlines()
    targets = [index + 1 for index, line in enumerate(lines) if "TRACE_TARGET" in line]
    if len(targets) != 1:
        raise AssertionError(f"{case.name}: expected one trace target, got {targets!r}")
    return script.replace("__TARGET_LINES__", repr(targets))


def _run_case(case: TraceCase, env_updates: dict[str, str]) -> subprocess.CompletedProcess[str]:
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
        "stderr_tail": proc.stderr.strip().splitlines()[-10:],
    }


def _result(proc: subprocess.CompletedProcess[str]) -> str | None:
    if proc.returncode != 0:
        return None
    lines = proc.stdout.strip().splitlines()
    if not lines:
        return None
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError:
        return None
    value = payload.get("result")
    return value if isinstance(value, str) else None


def main() -> int:
    modes = {
        "tier1": {"PYTHON_JIT": "0"},
        "tier2": {"PYTHON_JIT": "1"},
        "tier2_no_opt": {"PYTHON_JIT": "1", "PYTHON_UOPS_OPTIMIZE": "0"},
    }

    candidate_failures: list[dict[str, object]] = []
    known_failures: list[dict[str, object]] = []
    for case in CASES:
        runs = {mode: _run_case(case, env) for mode, env in modes.items()}
        results = {mode: _normalize(proc) for mode, proc in runs.items()}
        print(f"CASE {case.name}")
        print(json.dumps(results, sort_keys=True))

        baseline = results["tier1"]
        for mode, result in results.items():
            if mode != "tier1" and result != baseline:
                candidate_failures.append(
                    {"case": case.name, "mode": mode, "baseline": baseline, "result": result}
                )

        bad_modes = [
            {"mode": mode, "result": results[mode]}
            for mode, proc in runs.items()
            if _result(proc) != "caught"
        ]
        if bad_modes:
            bucket = known_failures if case.known_issue else candidate_failures
            bucket.append({"case": case.name, "known_issue": case.known_issue, "failures": bad_modes})

    if known_failures:
        print("KNOWN_FAILURES")
        print(json.dumps(known_failures, indent=2, sort_keys=True))

    if candidate_failures:
        print("TRACE_CANDIDATE_FAILURES")
        print(json.dumps(candidate_failures, indent=2, sort_keys=True))
        return 1

    print("trace injection suite: no candidate failures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
