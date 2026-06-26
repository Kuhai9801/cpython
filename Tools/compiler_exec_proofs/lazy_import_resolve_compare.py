#!/usr/bin/env python3
"""Validate LazyImportType.resolve() global reification semantics."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass


@dataclass(frozen=True)
class Case:
    name: str
    source: str
    expected: object


def _body(source: str) -> str:
    return textwrap.dedent(source).strip() + "\n"


CASES: list[Case] = [
    Case(
        "lazy_import_resolve_replaces_module_global_once",
        _body(
            """
            import builtins
            import json
            import types

            real_import = builtins.__import__
            calls = []

            lazy import target_module as target

            def custom_import(name, globals=None, locals=None, fromlist=None, level=0):
                if name == "target_module":
                    index = len(calls) + 1
                    fromlist_record = list(fromlist) if isinstance(fromlist, tuple) else fromlist
                    calls.append([index, name, globals.get("__name__"), fromlist_record])
                    module = types.ModuleType(name)
                    module.VALUE = f"value-{index}"
                    return module
                return real_import(name, globals, locals, fromlist, level)

            builtins.__import__ = custom_import
            try:
                def resolve_from_dict():
                    lazy_obj = globals()["target"]
                    resolved = lazy_obj.resolve()
                    return {
                        "resolved_value": resolved.VALUE,
                        "global_type_after_resolve": type(globals()["target"]).__name__,
                    }

                resolved = resolve_from_dict()
                direct_value = target.VALUE
                result = {
                    **resolved,
                    "direct_value": direct_value,
                    "global_type_after_direct": type(globals()["target"]).__name__,
                    "calls": calls,
                }
            finally:
                builtins.__import__ = real_import

            print(json.dumps({"result": result}, sort_keys=True))
            """
        ),
        {
            "resolved_value": "value-1",
            "global_type_after_resolve": "module",
            "direct_value": "value-1",
            "global_type_after_direct": "module",
            "calls": [[1, "target_module", "__main__", None]],
        },
    ),
    Case(
        "lazy_from_import_resolve_replaces_module_global_once",
        _body(
            """
            import builtins
            import json
            import types

            real_import = builtins.__import__
            calls = []

            lazy from target_module import VALUE as value

            def custom_import(name, globals=None, locals=None, fromlist=None, level=0):
                if name == "target_module":
                    index = len(calls) + 1
                    fromlist_record = list(fromlist) if isinstance(fromlist, tuple) else fromlist
                    calls.append([index, name, globals.get("__name__"), fromlist_record])
                    module = types.ModuleType(name)
                    module.VALUE = f"value-{index}"
                    return module
                return real_import(name, globals, locals, fromlist, level)

            builtins.__import__ = custom_import
            try:
                def resolve_from_dict():
                    lazy_obj = globals()["value"]
                    resolved = lazy_obj.resolve()
                    return {
                        "resolved_value": resolved,
                        "global_type_after_resolve": type(globals()["value"]).__name__,
                    }

                resolved = resolve_from_dict()
                direct_value = value
                result = {
                    **resolved,
                    "direct_value": direct_value,
                    "global_type_after_direct": type(globals()["value"]).__name__,
                    "calls": calls,
                }
            finally:
                builtins.__import__ = real_import

            print(json.dumps({"result": result}, sort_keys=True))
            """
        ),
        {
            "resolved_value": "value-1",
            "global_type_after_resolve": "str",
            "direct_value": "value-1",
            "global_type_after_direct": "str",
            "calls": [[1, "target_module", "__main__", ["VALUE"]]],
        },
    ),
]


def _run_case(case: Case, env_updates: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(env_updates)
    env.setdefault("PYTHONHASHSEED", "0")
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tmp:
        tmp.write(case.source)
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
    parsed = None
    lines = proc.stdout.strip().splitlines()
    if lines:
        try:
            parsed = json.loads(lines[-1])
        except json.JSONDecodeError:
            parsed = None
    return {
        "returncode": proc.returncode,
        "stdout": lines,
        "parsed": parsed,
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

    failures: list[dict[str, object]] = []
    for case in selected:
        results = {mode: _normalize(_run_case(case, env)) for mode, env in modes.items()}
        print(f"CASE {case.name}")
        print(json.dumps(results, sort_keys=True))
        for mode, result in results.items():
            parsed = result["parsed"]
            actual = parsed.get("result") if isinstance(parsed, dict) else None
            if result["returncode"] != 0 or actual != case.expected:
                failures.append(
                    {
                        "case": case.name,
                        "mode": mode,
                        "expected": case.expected,
                        "actual": actual,
                        "result": result,
                    }
                )

    if failures:
        print("FAILURES")
        print(json.dumps(failures, indent=2, sort_keys=True))
        return 1
    print("lazy import resolve suite: no failures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
