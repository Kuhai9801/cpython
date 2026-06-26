#!/usr/bin/env python3
"""Run deterministic source-level stress checks across execution modes."""

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
        "global_builtin_shadow_inside_hot_loop",
        _body(
            f"""
            def f(limit):
                total = 0
                for i in range(limit):
                    if i == {WARMUP}:
                        globals()["len"] = lambda value: 7
                    total += len([1, 2])
                return total

            try:
                del len
            except NameError:
                pass
            print(json.dumps({{"result": f({WARMUP + 16})}}))
            """
        ),
    ),
    Case(
        "builtins_dict_rebind_inside_hot_loop",
        _body(
            f"""
            src = '''
            def f(limit):
                total = 0
                for i in range(limit):
                    if i == {WARMUP}:
                        __builtins__["len"] = lambda value: 9
                    total += len([1, 2, 3])
                return total
            out = f({WARMUP + 16})
            '''
            builtins = {{"len": len, "range": range}}
            ns = {{"__builtins__": builtins}}
            exec(compile(src, "<stress-builtins>", "exec"), ns)
            print(json.dumps({{"result": ns["out"]}}))
            """
        ),
    ),
    Case(
        "class_method_replacement_inside_hot_loop",
        _body(
            f"""
            class C:
                def m(self):
                    return 1

            def f(obj, limit):
                total = 0
                for i in range(limit):
                    if i == {WARMUP}:
                        C.m = lambda self: 5
                    total += obj.m()
                return total

            print(json.dumps({{"result": f(C(), {WARMUP + 16})}}))
            """
        ),
    ),
    Case(
        "descriptor_get_replacement_inside_hot_loop",
        _body(
            f"""
            class D:
                def __get__(self, obj, owner):
                    return 2

            class C:
                x = D()

            def f(obj, limit):
                total = 0
                for i in range(limit):
                    if i == {WARMUP}:
                        D.__get__ = lambda self, obj, owner: 8
                    total += obj.x
                return total

            print(json.dumps({{"result": f(C(), {WARMUP + 16})}}))
            """
        ),
    ),
    Case(
        "property_replacement_inside_hot_loop",
        _body(
            f"""
            class C:
                @property
                def x(self):
                    return 3

            def f(obj, limit):
                total = 0
                for i in range(limit):
                    if i == {WARMUP}:
                        C.x = property(lambda self: 11)
                    total += obj.x
                return total

            print(json.dumps({{"result": f(C(), {WARMUP + 16})}}))
            """
        ),
    ),
    Case(
        "mro_base_replacement_inside_hot_loop",
        _body(
            f"""
            class A:
                x = 4

            class B:
                x = 13

            class C(A):
                pass

            def f(obj, limit):
                total = 0
                for i in range(limit):
                    if i == {WARMUP}:
                        C.__bases__ = (B,)
                    total += obj.x
                return total

            print(json.dumps({{"result": f(C(), {WARMUP + 16})}}))
            """
        ),
    ),
    Case(
        "callable_code_replacement_inside_hot_loop",
        _body(
            f"""
            def g(value):
                return value + 1

            def h(value):
                return value + 10

            def f(limit):
                total = 0
                for i in range(limit):
                    if i == {WARMUP}:
                        g.__code__ = h.__code__
                    total += g(1)
                return total

            print(json.dumps({{"result": f({WARMUP + 16})}}))
            """
        ),
    ),
    Case(
        "function_defaults_replacement_inside_hot_loop",
        _body(
            f"""
            def g(value=2):
                return value

            def f(limit):
                total = 0
                for i in range(limit):
                    if i == {WARMUP}:
                        g.__defaults__ = (12,)
                    total += g()
                return total

            print(json.dumps({{"result": f({WARMUP + 16})}}))
            """
        ),
    ),
    Case(
        "type_predicate_after_value_type_change",
        _body(
            f"""
            def f(limit):
                value = 1
                out = []
                for i in range(limit):
                    if i == {WARMUP}:
                        value = "s"
                    if type(value) is int:
                        out.append(["int", value + 1])
                    else:
                        out.append(["other", value + "!"])
                return out[-5:]

            print(json.dumps({{"result": f({WARMUP + 16})}}))
            """
        ),
    ),
    Case(
        "isinstance_predicate_after_value_type_change",
        _body(
            f"""
            def f(limit):
                value = 1
                out = []
                for i in range(limit):
                    if i == {WARMUP}:
                        value = []
                    if isinstance(value, int):
                        out.append(value + 1)
                    else:
                        value.append(i)
                        out.append(value[-1])
                return out[-5:]

            print(json.dumps({{"result": f({WARMUP + 16})}}))
            """
        ),
    ),
    Case(
        "float_equality_narrowing_preserves_negative_zero",
        _body(
            f"""
            import math

            def return_negative_zero():
                return -0.0

            def f(limit):
                value = return_negative_zero()
                out = []
                for _ in range(limit):
                    if value == 0.0:
                        out.append(math.copysign(1.0, value))
                    else:
                        out.append(0.0)
                return out[-8:]

            print(json.dumps({{"result": f({WARMUP})}}))
            """
        ),
    ),
    Case(
        "int_equality_narrowing_preserves_identity",
        _body(
            f"""
            def return_thousand():
                return int("1000")

            def f(limit):
                expected = 1000
                value = return_thousand()
                out = []
                for _ in range(limit):
                    if value == expected:
                        out.append(value is expected)
                    else:
                        out.append("miss")
                return out[-8:]

            print(json.dumps({{"result": f({WARMUP})}}))
            """
        ),
    ),
    Case(
        "str_equality_narrowing_preserves_identity",
        _body(
            f"""
            def return_string():
                return "".join(["not", "-", "interned"])

            def f(limit):
                expected = "not-interned"
                value = return_string()
                out = []
                for _ in range(limit):
                    if value == expected:
                        out.append(value is expected)
                    else:
                        out.append("miss")
                return out[-8:]

            print(json.dumps({{"result": f({WARMUP})}}))
            """
        ),
    ),
    Case(
        "match_args_replacement_inside_hot_loop",
        _body(
            f"""
            class C:
                __match_args__ = ("x",)

                def __init__(self):
                    self.x = 1
                    self.y = 6

            def f(obj, limit):
                total = 0
                for i in range(limit):
                    if i == {WARMUP}:
                        C.__match_args__ = ("y",)
                    match obj:
                        case C(value):
                            total += value
                        case _:
                            total -= 100
                return total

            print(json.dumps({{"result": f(C(), {WARMUP + 16})}}))
            """
        ),
    ),
    Case(
        "list_append_during_virtual_iteration_hot_loop",
        _body(
            f"""
            def f(limit):
                total = 0
                for _ in range(limit):
                    seq = [1, 2]
                    seen = []
                    for value in seq:
                        seen.append(value)
                        if value == 1:
                            seq.append(3)
                    total += sum(seen) * len(seen)
                return total

            print(json.dumps({{"result": f({WARMUP + 16})}}))
            """
        ),
    ),
    Case(
        "list_shrink_during_virtual_iteration_hot_loop",
        _body(
            f"""
            def f(limit):
                total = 0
                for _ in range(limit):
                    seq = [1, 2, 3]
                    seen = []
                    for value in seq:
                        seen.append(value)
                        if value == 1:
                            seq.pop()
                    total += sum(seen) * len(seen)
                return total

            print(json.dumps({{"result": f({WARMUP + 16})}}))
            """
        ),
    ),
    Case(
        "super_global_shadow_inside_hot_loop",
        _body(
            f"""
            class A:
                def m(self):
                    return 1

            class C(A):
                def f(self):
                    return super().m()

            class Proxy:
                def m(self):
                    return 10

            def drive(obj, limit):
                total = 0
                for i in range(limit):
                    if i == {WARMUP}:
                        globals()["super"] = lambda *args: Proxy()
                    total += obj.f()
                return total

            try:
                del super
            except NameError:
                pass
            print(json.dumps({{"result": drive(C(), {WARMUP + 16})}}))
            """
        ),
    ),
    Case(
        "module_attr_replacement_inside_hot_loop",
        _body(
            f"""
            import types

            module = types.ModuleType("stress_module")
            module.x = 2

            def f(limit):
                total = 0
                for i in range(limit):
                    if i == {WARMUP}:
                        module.x = 14
                    total += module.x
                return total

            print(json.dumps({{"result": f({WARMUP + 16})}}))
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
    print("stress suite: no mismatches")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
