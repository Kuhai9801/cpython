#!/usr/bin/env python3
"""Validate dotted lazy-import reification across execution modes."""

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
        "lazy_dotted_import_uses_full_name_import_hook_for_parent_attribute",
        _body(
            """
            import builtins
            import json
            import os
            import sys
            import tempfile
            import textwrap
            import types

            files = {
                "hookpkg/__init__.py": "",
                "hookpkg/sub.py": 'VALUE = "real-submodule"\\n',
            }

            with tempfile.TemporaryDirectory() as tmpdir:
                for relpath, contents in files.items():
                    path = os.path.join(tmpdir, relpath)
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, "w", encoding="utf-8") as file:
                        file.write(textwrap.dedent(contents).lstrip())

                calls = []
                real_import = builtins.__import__

                def recording_import(name, globals=None, locals=None,
                                     fromlist=(), level=0):
                    if name == "hookpkg.sub":
                        importer = None if globals is None else globals.get("__name__")
                        calls.append([name, importer, list(fromlist or ())])
                        pkg = real_import("hookpkg", globals, locals, (), level)
                        pkg.sub = types.SimpleNamespace(VALUE="hooked-submodule")
                        return pkg
                    return real_import(name, globals, locals, fromlist, level)

                sys.path.insert(0, tmpdir)
                builtins.__import__ = recording_import
                lazy import hookpkg.sub
                value = hookpkg.sub.VALUE
                builtins.__import__ = real_import
                sys.path.remove(tmpdir)

            print(json.dumps({"result": {"value": value, "calls": calls}}, sort_keys=True))
            """
        ),
        {"value": "hooked-submodule", "calls": [["hookpkg.sub", "__main__", []]]},
    ),
    Case(
        "lazy_dotted_as_alias_uses_full_name_import_hook",
        _body(
            """
            import builtins
            import json
            import os
            import sys
            import tempfile
            import textwrap
            import types

            files = {
                "hookpkg/__init__.py": "",
                "hookpkg/sub.py": 'VALUE = "real-submodule"\\n',
            }

            with tempfile.TemporaryDirectory() as tmpdir:
                for relpath, contents in files.items():
                    path = os.path.join(tmpdir, relpath)
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, "w", encoding="utf-8") as file:
                        file.write(textwrap.dedent(contents).lstrip())

                calls = []
                real_import = builtins.__import__

                def recording_import(name, globals=None, locals=None,
                                     fromlist=(), level=0):
                    if name == "hookpkg.sub":
                        importer = None if globals is None else globals.get("__name__")
                        calls.append([name, importer, list(fromlist or ())])
                        pkg = real_import("hookpkg", globals, locals, (), level)
                        pkg.sub = types.SimpleNamespace(VALUE="hooked-submodule")
                        return pkg.sub
                    return real_import(name, globals, locals, fromlist, level)

                sys.path.insert(0, tmpdir)
                builtins.__import__ = recording_import
                lazy import hookpkg.sub as alias
                value = alias.VALUE
                builtins.__import__ = real_import
                sys.path.remove(tmpdir)

            print(json.dumps({"result": {"value": value, "calls": calls}}, sort_keys=True))
            """
        ),
        {"value": "hooked-submodule", "calls": [["hookpkg.sub", "__main__", []]]},
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
    print("lazy dotted import suite: no failures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
