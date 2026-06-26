#!/usr/bin/env python3
"""Run generated source-level compiler/runtime differential checks."""

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


def _body(source: str) -> str:
    return textwrap.dedent(source).strip() + "\n"


def _program(source: str) -> str:
    return textwrap.dedent(source).strip() + "\n"


PROGRAMS = [
    _program(
        """
        def sample(limit):
            out = []
            for i in range(limit):
                try:
                    if i == 1:
                        continue
                    if i == 2:
                        raise ValueError("v")
                    if i == 3:
                        break
                    out.append(("body", i))
                except ValueError as exc:
                    out.append(("except", str(exc)))
                finally:
                    out.append(("finally", i))
            else:
                out.append("else")
            return out

        result = sample(5)
        """
    ),
    _program(
        """
        def outer():
            x = 10

            def bump(v):
                nonlocal x
                x += v
                return x

            values = [(i, bump(i)) for i in range(5) if i % 2 == 0 or bump(0)]
            return values, x

        result = outer()
        """
    ),
    _program(
        """
        def gen():
            events = []
            try:
                received = yield "start"
                events.append(("received", received))
                yield "middle"
            except KeyError as exc:
                events.append(("caught", exc.args[0]))
                yield "handled"
            finally:
                events.append("finally")
            return events

        it = gen()
        out = [next(it)]
        out.append(it.throw(KeyError("k")))
        try:
            next(it)
        except StopIteration as exc:
            out.append(exc.value)
        result = out
        """
    ),
    _program(
        """
        class Point:
            __match_args__ = ("x", "y")

            def __init__(self, x, y):
                self.x = x
                self.y = y

        def classify(obj):
            match obj:
                case Point(x, y) if x == y:
                    return ("diag", x + y)
                case Point(x, y):
                    return ("point", x - y)
                case {"kind": "pair", "items": [a, b]}:
                    return ("pair", a * b)
                case _:
                    return "miss"

        result = [
            classify(Point(3, 3)),
            classify(Point(5, 2)),
            classify({"kind": "pair", "items": [6, 7]}),
            classify(None),
        ]
        """
    ),
    _program(
        """
        out = []
        try:
            raise ExceptionGroup(
                "root",
                [ValueError("v1"), TypeError("t"), ValueError("v2")],
            )
        except* ValueError as eg:
            out.append(("value", [type(e).__name__ for e in eg.exceptions]))
        except* Exception as eg:
            out.append(("other", [type(e).__name__ for e in eg.exceptions]))
        result = out
        """
    ),
    _program(
        """
        async def source():
            for item in (1, 2, 3):
                yield item

        async def collect():
            out = []
            async for item in source():
                try:
                    out.append(item * 10)
                finally:
                    out.append("finally")
            return out

        coro = collect()
        try:
            coro.send(None)
        except StopIteration as exc:
            result = exc.value
        """
    ),
]


CASES = [
    Case(
        "compile_ast_marshal_equivalence",
        _body(
            """
            import ast
            import json
            import marshal

            PROGRAMS = __PROGRAMS__

            def canon(value):
                if isinstance(value, tuple):
                    return [canon(item) for item in value]
                if isinstance(value, list):
                    return [canon(item) for item in value]
                if isinstance(value, dict):
                    return {str(key): canon(val) for key, val in sorted(value.items())}
                return value

            def run(src, variant):
                if variant == "source":
                    code = compile(src, "<generated-source>", "exec")
                elif variant == "ast":
                    code = compile(ast.parse(src, mode="exec"), "<generated-ast>", "exec")
                elif variant == "marshal":
                    code = marshal.loads(marshal.dumps(compile(src, "<generated-marshal>", "exec")))
                else:
                    raise AssertionError(variant)
                ns = {}
                exec(code, ns)
                return canon(ns["result"])

            failures = []
            for index, src in enumerate(PROGRAMS):
                results = {variant: run(src, variant) for variant in ("source", "ast", "marshal")}
                if len({json.dumps(value, sort_keys=True) for value in results.values()}) != 1:
                    failures.append({"index": index, "results": results, "source": src})

            print(json.dumps({"result": "ok" if not failures else failures}, sort_keys=True))
            raise SystemExit(1 if failures else 0)
            """.replace("__PROGRAMS__", repr(PROGRAMS))
        ),
    ),
    Case(
        "optimize_equivalence_without_debug_sensitive_constructs",
        _body(
            """
            import json

            PROGRAMS = __PROGRAMS__

            def canon(value):
                if isinstance(value, tuple):
                    return [canon(item) for item in value]
                if isinstance(value, list):
                    return [canon(item) for item in value]
                if isinstance(value, dict):
                    return {str(key): canon(val) for key, val in sorted(value.items())}
                return value

            def run(src, optimize):
                ns = {}
                exec(compile(src, f"<generated-opt-{optimize}>", "exec", optimize=optimize), ns)
                return canon(ns["result"])

            failures = []
            for index, src in enumerate(PROGRAMS):
                results = {str(optimize): run(src, optimize) for optimize in (0, 1, 2)}
                if len({json.dumps(value, sort_keys=True) for value in results.values()}) != 1:
                    failures.append({"index": index, "results": results, "source": src})

            print(json.dumps({"result": "ok" if not failures else failures}, sort_keys=True))
            raise SystemExit(1 if failures else 0)
            """.replace("__PROGRAMS__", repr(PROGRAMS))
        ),
    ),
    Case(
        "lazy_import_reification_module_scope",
        _body(
            """
            import json
            import pathlib
            import sys
            import tempfile

            with tempfile.TemporaryDirectory() as root:
                path = pathlib.Path(root)
                (path / "lazy_target.py").write_text("value = 41\\n", encoding="utf-8")
                sys.path.insert(0, root)
                try:
                    ns = {}
                    source = '''
            import sys
            lazy import lazy_target
            result = ("lazy_target" in sys.modules, lazy_target.value, "lazy_target" in sys.modules)
            '''
                    exec(compile(source, "<lazy-import-case>", "exec"), ns)
                    result = ns["result"]
                finally:
                    sys.path.remove(root)
                    sys.modules.pop("lazy_target", None)

            print(json.dumps({"result": result}, sort_keys=True))
            """
        ),
    ),
    Case(
        "lazy_from_import_reification_module_scope",
        _body(
            """
            import json
            import pathlib
            import sys
            import tempfile

            with tempfile.TemporaryDirectory() as root:
                path = pathlib.Path(root)
                (path / "lazy_from_target.py").write_text("value = 11\\nother = 22\\n", encoding="utf-8")
                sys.path.insert(0, root)
                try:
                    ns = {}
                    source = '''
            import sys
            lazy from lazy_from_target import value, other
            result = (
                "lazy_from_target" in sys.modules,
                value,
                "lazy_from_target" in sys.modules,
                other,
            )
            '''
                    exec(compile(source, "<lazy-from-case>", "exec"), ns)
                    result = ns["result"]
                finally:
                    sys.path.remove(root)
                    sys.modules.pop("lazy_from_target", None)

            print(json.dumps({"result": result}, sort_keys=True))
            """
        ),
    ),
]


def _script_for(case: Case) -> str:
    prelude = """
import os
import sys

def _proof_trace(frame, event, arg):
    return _proof_trace

def _proof_profile(frame, event, arg):
    return None

if os.environ.get("PROOF_TRACE"):
    sys.settrace(_proof_trace)
if os.environ.get("PROOF_PROFILE"):
    sys.setprofile(_proof_profile)
"""
    return textwrap.dedent(prelude).strip() + "\n" + case.source


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
            timeout=30,
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
        "stderr_tail": proc.stderr.strip().splitlines()[-12:],
    }


def main() -> int:
    case_filter = os.environ.get("GENERATED_CASE_FILTER", "")
    modes = {
        "tier1": {"PYTHON_JIT": "0"},
        "tier2": {"PYTHON_JIT": "1"},
        "tier2_no_opt": {"PYTHON_JIT": "1", "PYTHON_UOPS_OPTIMIZE": "0"},
        "trace_tier1": {"PYTHON_JIT": "0", "PROOF_TRACE": "1"},
        "trace_tier2": {"PYTHON_JIT": "1", "PROOF_TRACE": "1"},
        "profile_tier2": {"PYTHON_JIT": "1", "PROOF_PROFILE": "1"},
    }

    selected = [case for case in CASES if case_filter in case.name]
    if not selected:
        raise SystemExit(f"no generated cases selected by filter {case_filter!r}")

    failures: list[dict[str, object]] = []
    for case in selected:
        runs = {mode: _run_case(case, env) for mode, env in modes.items()}
        results = {mode: _normalize(proc) for mode, proc in runs.items()}
        print(f"CASE {case.name}")
        print(json.dumps(results, sort_keys=True))
        baseline = results["tier1"]
        for mode, result in results.items():
            if result["returncode"] != 0:
                failures.append({"case": case.name, "mode": mode, "failure": result})
            elif mode != "tier1" and result != baseline:
                failures.append({"case": case.name, "mode": mode, "baseline": baseline, "result": result})

    if failures:
        print("GENERATED_FAILURES")
        print(json.dumps(failures, indent=2, sort_keys=True))
        return 1

    print("generated suite: no mismatches")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
