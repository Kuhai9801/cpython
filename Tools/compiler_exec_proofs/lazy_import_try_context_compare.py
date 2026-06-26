#!/usr/bin/env python3
"""Validate lazy-import eager forcing in try-statement sub-blocks."""

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
        "global_lazy_imports_finally_paths_are_eager",
        _body(
            """
            import json
            import os
            import sys
            import tempfile
            import textwrap

            files = {
                "fin_normal.py": 'VALUE = "normal-finally"\\n',
                "fin_error.py": 'VALUE = "error-finally"\\n',
            }

            with tempfile.TemporaryDirectory() as tmpdir:
                for relpath, contents in files.items():
                    path = os.path.join(tmpdir, relpath)
                    with open(path, "w", encoding="utf-8") as file:
                        file.write(textwrap.dedent(contents).lstrip())

                sys.path.insert(0, tmpdir)
                sys.set_lazy_imports("all")
                try:
                    try:
                        pass
                    finally:
                        import fin_normal

                    try:
                        try:
                            raise RuntimeError("force exceptional finally")
                        finally:
                            import fin_error
                    except RuntimeError:
                        pass

                    result = {
                        "normal_loaded": "fin_normal" in sys.modules,
                        "error_loaded": "fin_error" in sys.modules,
                    }
                finally:
                    sys.set_lazy_imports("normal")
                    sys.path.remove(tmpdir)

            print(json.dumps({"result": result}, sort_keys=True))
            """
        ),
        {"normal_loaded": True, "error_loaded": True},
    ),
    Case(
        "lazy_modules_finally_import_is_eager",
        _body(
            """
            import json
            import os
            import sys
            import tempfile
            import textwrap

            files = {
                "compat_finally.py": 'VALUE = "compat-finally"\\n',
            }

            with tempfile.TemporaryDirectory() as tmpdir:
                for relpath, contents in files.items():
                    path = os.path.join(tmpdir, relpath)
                    with open(path, "w", encoding="utf-8") as file:
                        file.write(textwrap.dedent(contents).lstrip())

                sys.path.insert(0, tmpdir)
                __lazy_modules__ = ["compat_finally"]
                try:
                    try:
                        pass
                    finally:
                        import compat_finally
                    result = "compat_finally" in sys.modules
                finally:
                    sys.path.remove(tmpdir)

            print(json.dumps({"result": result}, sort_keys=True))
            """
        ),
        True,
    ),
    Case(
        "global_lazy_imports_try_else_import_is_eager",
        _body(
            """
            import json
            import os
            import sys
            import tempfile
            import textwrap

            files = {
                "else_target.py": 'VALUE = "try-else"\\n',
            }

            with tempfile.TemporaryDirectory() as tmpdir:
                for relpath, contents in files.items():
                    path = os.path.join(tmpdir, relpath)
                    with open(path, "w", encoding="utf-8") as file:
                        file.write(textwrap.dedent(contents).lstrip())

                sys.path.insert(0, tmpdir)
                sys.set_lazy_imports("all")
                try:
                    try:
                        pass
                    except RuntimeError:
                        pass
                    else:
                        import else_target
                    result = "else_target" in sys.modules
                finally:
                    sys.set_lazy_imports("normal")
                    sys.path.remove(tmpdir)

            print(json.dumps({"result": result}, sort_keys=True))
            """
        ),
        True,
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
    print("lazy import try-context suite: no failures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
