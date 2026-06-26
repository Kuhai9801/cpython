#!/usr/bin/env python3
"""Run synthesized source-level compiler/runtime differential checks."""

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
        "global_shadow_str_after_call_str_warmup",
        _body(
            f"""
            def f(value):
                return str(value)

            for _ in range({WARMUP}):
                f(123)

            str = lambda value: ["shadow-str", value]
            print(json.dumps({{"result": [f(i) for i in range(8)]}}))
            """
        ),
    ),
    Case(
        "global_shadow_tuple_after_call_tuple_warmup",
        _body(
            f"""
            def f(value):
                return tuple(value)

            for _ in range({WARMUP}):
                f([1, 2])

            tuple = lambda value: ["shadow-tuple", list(value)]
            print(json.dumps({{"result": [f([i, i + 1]) for i in range(8)]}}))
            """
        ),
    ),
    Case(
        "global_shadow_type_after_call_type_warmup",
        _body(
            f"""
            def f(value):
                return type(value)

            for _ in range({WARMUP}):
                f(1)

            type = lambda value: ["shadow-type", value]
            print(json.dumps({{"result": [f(i) for i in range(8)]}}))
            """
        ),
    ),
    Case(
        "global_delete_reveals_builtin_str_after_call_str_warmup",
        _body(
            f"""
            str = lambda value: ["shadow-str", value]

            def f(value):
                return str(value)

            for _ in range({WARMUP}):
                f(123)

            del str
            print(json.dumps({{"result": [f(i) for i in range(8)]}}))
            """
        ),
    ),
    Case(
        "global_delete_reveals_builtin_tuple_after_call_tuple_warmup",
        _body(
            f"""
            tuple = lambda value: ["shadow-tuple", list(value)]

            def f(value):
                return tuple(value)

            for _ in range({WARMUP}):
                f([1, 2])

            del tuple
            print(json.dumps({{"result": [list(f([i, i + 1])) for i in range(8)]}}))
            """
        ),
    ),
    Case(
        "global_shadow_super_after_load_super_attr_warmup",
        _body(
            f"""
            class A:
                def m(self):
                    return "base"

            class C(A):
                def f(self):
                    return super().m()

            class Proxy:
                def m(self):
                    return "proxy"

            c = C()
            for _ in range({WARMUP}):
                c.f()

            super = lambda *args: Proxy()
            print(json.dumps({{"result": [c.f() for _ in range(8)]}}))
            """
        ),
    ),
    Case(
        "staticmethod_replacement_after_class_call_warmup",
        _body(
            f"""
            class C:
                @staticmethod
                def m(value):
                    return ["old", value]

            def f(value):
                return C.m(value)

            for _ in range({WARMUP}):
                f(1)

            C.m = staticmethod(lambda value: ["new", value])
            print(json.dumps({{"result": [f(i) for i in range(8)]}}))
            """
        ),
    ),
    Case(
        "classmethod_replacement_after_class_call_warmup",
        _body(
            f"""
            class C:
                @classmethod
                def m(cls, value):
                    return [cls.__name__, "old", value]

            def f(value):
                return C.m(value)

            for _ in range({WARMUP}):
                f(1)

            C.m = classmethod(lambda cls, value: [cls.__name__, "new", value])
            print(json.dumps({{"result": [f(i) for i in range(8)]}}))
            """
        ),
    ),
    Case(
        "base_method_replacement_after_inherited_call_warmup",
        _body(
            f"""
            class Base:
                def m(self, value):
                    return ["old", value]

            class C(Base):
                pass

            def f(obj, value):
                return obj.m(value)

            c = C()
            for _ in range({WARMUP}):
                f(c, 1)

            Base.m = lambda self, value: ["new", value]
            print(json.dumps({{"result": [f(c, i) for i in range(8)]}}))
            """
        ),
    ),
    Case(
        "descriptor_delete_reveals_instance_dict_after_warmup",
        _body(
            f"""
            class C:
                @property
                def x(self):
                    return "property"

            def f(obj):
                return obj.x

            c = C()
            c.__dict__["x"] = "dict"
            for _ in range({WARMUP}):
                f(c)

            del C.x
            print(json.dumps({{"result": [f(c) for _ in range(8)]}}))
            """
        ),
    ),
    Case(
        "kwdefaults_replacement_after_kw_call_warmup",
        _body(
            f"""
            def g(*, value=1):
                return value

            def f():
                return g()

            for _ in range({WARMUP}):
                f()

            g.__kwdefaults__ = {{"value": 2}}
            print(json.dumps({{"result": [f() for _ in range(8)]}}))
            """
        ),
    ),
    Case(
        "method_function_code_replacement_after_bound_call_warmup",
        _body(
            f"""
            class C:
                def m(self, value):
                    return ["old", value]

            def replacement(self, value):
                return ["new", value]

            def f(obj, value):
                return obj.m(value)

            c = C()
            for _ in range({WARMUP}):
                f(c, 1)

            C.m.__code__ = replacement.__code__
            print(json.dumps({{"result": [f(c, i) for i in range(8)]}}))
            """
        ),
    ),
    Case(
        "closure_callable_rebinding_after_warmup",
        _body(
            f"""
            def make():
                def old(value):
                    return ["old", value]

                def new(value):
                    return ["new", value]

                func = old

                def f(value):
                    return func(value)

                def swap():
                    nonlocal func
                    func = new

                return f, swap

            f, swap = make()
            for _ in range({WARMUP}):
                f(1)

            swap()
            print(json.dumps({{"result": [f(i) for i in range(8)]}}))
            """
        ),
    ),
    Case(
        "match_args_replacement_after_match_warmup",
        _body(
            f"""
            class C:
                __match_args__ = ("x",)

                def __init__(self):
                    self.x = "x"
                    self.y = "y"

            def f(obj):
                match obj:
                    case C(value):
                        return value
                    case _:
                        return "miss"

            c = C()
            for _ in range({WARMUP}):
                f(c)

            C.__match_args__ = ("y",)
            print(json.dumps({{"result": [f(c) for _ in range(8)]}}))
            """
        ),
    ),
    Case(
        "match_class_getattribute_replacement_after_warmup",
        _body(
            f"""
            class C:
                __match_args__ = ("x",)
                x = "class"

            def f(obj):
                match obj:
                    case C(value):
                        return value
                    case _:
                        return "miss"

            c = C()
            for _ in range({WARMUP}):
                f(c)

            def custom_getattribute(self, name):
                if name == "x":
                    return "custom"
                return object.__getattribute__(self, name)

            C.__getattribute__ = custom_getattribute
            print(json.dumps({{"result": [f(c) for _ in range(8)]}}))
            """
        ),
    ),
    Case(
        "except_star_nested_finally_after_warmup",
        _body(
            f"""
            def f():
                out = []
                try:
                    try:
                        raise ExceptionGroup("root", [ValueError("v"), TypeError("t")])
                    except* ValueError as eg:
                        out.append(["value", [type(e).__name__ for e in eg.exceptions]])
                    except* Exception as eg:
                        out.append(["other", [type(e).__name__ for e in eg.exceptions]])
                    finally:
                        out.append("finally")
                except Exception as exc:
                    out.append(["outer", type(exc).__name__])
                return out

            for _ in range({WARMUP}):
                f()

            print(json.dumps({{"result": f()}}))
            """
        ),
    ),
    Case(
        "generator_throw_after_next_warmup",
        _body(
            f"""
            def gen():
                out = []
                try:
                    out.append((yield "start"))
                except KeyError as exc:
                    out.append(["caught", exc.args[0]])
                    yield out
                finally:
                    out.append("finally")

            def drive_throw(value):
                iterator = gen()
                first = next(iterator)
                second = iterator.throw(KeyError(value))
                try:
                    next(iterator)
                except StopIteration:
                    done = True
                else:
                    done = False
                return [first, second, done]

            for _ in range({WARMUP}):
                drive_throw("warm")

            print(json.dumps({{"result": [drive_throw(i) for i in range(8)]}}))
            """
        ),
    ),
    Case(
        "generator_close_finally_after_yield_warmup",
        _body(
            f"""
            events = []

            def gen(label):
                try:
                    yield ["yield", label]
                finally:
                    events.append(["finally", label])

            def drive(label):
                iterator = gen(label)
                first = next(iterator)
                iterator.close()
                return first

            for _ in range({WARMUP}):
                drive("warm")
                events.clear()

            result = [drive(i) for i in range(8)]
            print(json.dumps({{"result": [result, events]}}))
            """
        ),
    ),
    Case(
        "async_generator_anext_after_warmup",
        _body(
            f"""
            async def source(value):
                yield ["first", value]
                yield ["second", value]

            async def collect(value):
                out = []
                async for item in source(value):
                    out.append(item)
                return out

            def drive(value):
                coro = collect(value)
                try:
                    coro.send(None)
                except StopIteration as exc:
                    return exc.value

            for _ in range({WARMUP}):
                drive("warm")

            print(json.dumps({{"result": [drive(i) for i in range(8)]}}))
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
    print("synthesized suite: no mismatches")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
