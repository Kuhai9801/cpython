#!/usr/bin/env python3
"""Compare CPython compiler/runtime semantics across tier-1 and tier-2 modes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass


WARMUP = 800


@dataclass(frozen=True)
class Case:
    name: str
    source: str


def _body(source: str) -> str:
    return textwrap.dedent(source).strip() + "\n"


CASES: list[Case] = [
    Case(
        "descriptor_get_mutation_after_load_attr_warmup",
        _body(
            f"""
            class D:
                def __get__(self, obj, owner):
                    return 1

            class C:
                x = D()

            def f(o):
                return o.x

            c = C()
            for _ in range({WARMUP}):
                f(c)

            def replacement(self, obj, owner):
                return 2

            D.__get__ = replacement
            print(json.dumps({{"result": [f(c) for _ in range(8)]}}))
            """
        ),
    ),
    Case(
        "property_fget_mutation_after_load_attr_warmup",
        _body(
            f"""
            class C:
                @property
                def x(self):
                    return 10

            def f(o):
                return o.x

            c = C()
            for _ in range({WARMUP}):
                f(c)

            C.x = property(lambda self: 20)
            print(json.dumps({{"result": [f(c) for _ in range(8)]}}))
            """
        ),
    ),
    Case(
        "getattribute_mutation_after_load_attr_warmup",
        _body(
            f"""
            class C:
                x = 3

            def f(o):
                return o.x

            c = C()
            for _ in range({WARMUP}):
                f(c)

            def custom_getattribute(self, name):
                if name == "x":
                    return 4
                return object.__getattribute__(self, name)

            C.__getattribute__ = custom_getattribute
            print(json.dumps({{"result": [f(c) for _ in range(8)]}}))
            """
        ),
    ),
    Case(
        "base_class_descriptor_mutation_after_warmup",
        _body(
            f"""
            class D:
                def __get__(self, obj, owner):
                    return "before"

            class Base:
                x = D()

            class C(Base):
                pass

            def f(o):
                return o.x

            c = C()
            for _ in range({WARMUP}):
                f(c)

            Base.x = "after"
            print(json.dumps({{"result": [f(c) for _ in range(8)]}}))
            """
        ),
    ),
    Case(
        "globals_reexec_with_distinct_builtins",
        _body(
            f"""
            src = '''
            def f():
                total = 0
                for i in range(80):
                    total += len([i])
                return total
            out = f()
            '''

            code = compile(src, "<generated>", "exec")
            ns1 = {{"__builtins__": {{"range": range, "len": lambda x: 10}}}}
            exec(code, ns1)

            ns2 = {{"__builtins__": {{"range": range, "len": lambda x: 20}}}}
            for _ in range({WARMUP}):
                exec(code, ns1)
            exec(code, ns2)

            print(json.dumps({{"result": [ns1["out"], ns2["out"]]}}))
            """
        ),
    ),
    Case(
        "cell_rebinding_after_closure_warmup",
        _body(
            f"""
            def make():
                x = 1
                def f():
                    total = 0
                    for _ in range(80):
                        total += x
                    return total
                def setx(v):
                    nonlocal x
                    x = v
                return f, setx

            f, setx = make()
            for _ in range({WARMUP}):
                f()
            setx(2)
            print(json.dumps({{"result": [f() for _ in range(8)]}}))
            """
        ),
    ),
    Case(
        "class_scope_same_name_closure_runtime",
        _body(
            """
            x = "global"

            def outer():
                x = "closure"
                class C:
                    x = x
                    y = x
                return C.x, C.y

            print(json.dumps({"result": outer()}))
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
                mismatches.append({"case": case.name, "mode": mode, "baseline": baseline, "result": result})

    if mismatches:
        print("MISMATCHES")
        print(json.dumps(mismatches, indent=2, sort_keys=True))
        return 1
    print("no mismatches")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
