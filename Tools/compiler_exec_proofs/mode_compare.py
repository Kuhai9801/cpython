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
    expected: object | None = None


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
        [2] * 8,
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
        [20] * 8,
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
        [4] * 8,
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
        ["after"] * 8,
    ),
    Case(
        "mro_base_replacement_after_load_attr_warmup",
        _body(
            f"""
            class A:
                x = "a"

            class B:
                x = "b"

            class C(A):
                pass

            def f(o):
                return o.x

            c = C()
            for _ in range({WARMUP}):
                f(c)

            C.__bases__ = (B,)
            print(json.dumps({{"result": [f(c) for _ in range(8)]}}))
            """
        ),
        ["b"] * 8,
    ),
    Case(
        "mro_base_replacement_after_isinstance_warmup",
        _body(
            f"""
            class A:
                pass

            class B:
                pass

            class C(A):
                pass

            def f(o):
                return isinstance(o, A), isinstance(o, B)

            c = C()
            for _ in range({WARMUP}):
                f(c)

            C.__bases__ = (B,)
            print(json.dumps({{"result": [f(c) for _ in range(8)]}}))
            """
        ),
        [[False, True]] * 8,
    ),
    Case(
        "super_mro_base_replacement_after_warmup",
        _body(
            f"""
            class A:
                def m(self):
                    return "a"

            class B:
                def m(self):
                    return "b"

            class C(A):
                def f(self):
                    return super().m()

            c = C()
            for _ in range({WARMUP}):
                c.f()

            C.__bases__ = (B,)
            print(json.dumps({{"result": [c.f() for _ in range(8)]}}))
            """
        ),
        ["b"] * 8,
    ),
    Case(
        "super_descriptor_replacement_after_warmup",
        _body(
            f"""
            class A:
                def m(self):
                    return "before"

            class C(A):
                def f(self):
                    return super().m()

            c = C()
            for _ in range({WARMUP}):
                c.f()

            A.m = lambda self: "after"
            print(json.dumps({{"result": [c.f() for _ in range(8)]}}))
            """
        ),
        ["after"] * 8,
    ),
    Case(
        "abc_registration_after_isinstance_warmup",
        _body(
            f"""
            import abc

            class A(metaclass=abc.ABCMeta):
                pass

            class B:
                pass

            def f(o):
                return isinstance(o, A)

            b = B()
            for _ in range({WARMUP}):
                f(b)

            A.register(B)
            print(json.dumps({{"result": [f(b) for _ in range(8)]}}))
            """
        ),
        [True] * 8,
    ),
    Case(
        "instancecheck_replacement_after_isinstance_warmup",
        _body(
            f"""
            class Meta(type):
                def __instancecheck__(cls, obj):
                    return False

            class A(metaclass=Meta):
                pass

            class B:
                pass

            def f(o):
                return isinstance(o, A)

            b = B()
            for _ in range({WARMUP}):
                f(b)

            Meta.__instancecheck__ = lambda cls, obj: True
            print(json.dumps({{"result": [f(b) for _ in range(8)]}}))
            """
        ),
        [True] * 8,
    ),
    Case(
        "method_replacement_after_load_method_warmup",
        _body(
            f"""
            class C:
                def m(self):
                    return 1

            def f(o):
                return o.m()

            c = C()
            for _ in range({WARMUP}):
                f(c)

            C.m = lambda self: 2
            print(json.dumps({{"result": [f(c) for _ in range(8)]}}))
            """
        ),
        [2] * 8,
    ),
    Case(
        "call_slot_replacement_after_call_warmup",
        _body(
            f"""
            class C:
                def __call__(self):
                    return 5

            def f(o):
                return o()

            c = C()
            for _ in range({WARMUP}):
                f(c)

            C.__call__ = lambda self: 6
            print(json.dumps({{"result": [f(c) for _ in range(8)]}}))
            """
        ),
        [6] * 8,
    ),
    Case(
        "len_slot_replacement_after_builtin_call_warmup",
        _body(
            f"""
            class C:
                def __len__(self):
                    return 3

            def f(o):
                return len(o)

            c = C()
            for _ in range({WARMUP}):
                f(c)

            C.__len__ = lambda self: 4
            print(json.dumps({{"result": [f(c) for _ in range(8)]}}))
            """
        ),
        [4] * 8,
    ),
    Case(
        "contains_slot_replacement_after_compare_warmup",
        _body(
            f"""
            class C:
                def __contains__(self, item):
                    return False

            def f(o):
                return 1 in o

            c = C()
            for _ in range({WARMUP}):
                f(c)

            C.__contains__ = lambda self, item: True
            print(json.dumps({{"result": [f(c) for _ in range(8)]}}))
            """
        ),
        [True] * 8,
    ),
    Case(
        "getitem_slot_replacement_after_subscr_warmup",
        _body(
            f"""
            class C:
                def __getitem__(self, item):
                    return item + 1

            def f(o):
                return o[10]

            c = C()
            for _ in range({WARMUP}):
                f(c)

            C.__getitem__ = lambda self, item: item + 2
            print(json.dumps({{"result": [f(c) for _ in range(8)]}}))
            """
        ),
        [12] * 8,
    ),
    Case(
        "binary_slot_replacement_after_binary_op_warmup",
        _body(
            f"""
            class C:
                def __add__(self, other):
                    return 1

            def f(o):
                return o + 10

            c = C()
            for _ in range({WARMUP}):
                f(c)

            C.__add__ = lambda self, other: 2
            print(json.dumps({{"result": [f(c) for _ in range(8)]}}))
            """
        ),
        [2] * 8,
    ),
    Case(
        "richcompare_slot_replacement_after_compare_warmup",
        _body(
            f"""
            class C:
                def __lt__(self, other):
                    return False

            def f(o):
                return o < 10

            c = C()
            for _ in range({WARMUP}):
                f(c)

            C.__lt__ = lambda self, other: True
            print(json.dumps({{"result": [f(c) for _ in range(8)]}}))
            """
        ),
        [True] * 8,
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
        [800, 1600],
    ),
    Case(
        "global_shadow_builtin_after_load_global_warmup",
        _body(
            f"""
            def f():
                return len([1])

            for _ in range({WARMUP}):
                f()

            len = lambda obj: 7
            print(json.dumps({{"result": [f() for _ in range(8)]}}))
            """
        ),
        [7] * 8,
    ),
    Case(
        "global_delete_reveals_builtin_after_load_global_warmup",
        _body(
            f"""
            len = lambda obj: 7

            def f():
                return len([1])

            for _ in range({WARMUP}):
                f()

            del len
            print(json.dumps({{"result": [f() for _ in range(8)]}}))
            """
        ),
        [1] * 8,
    ),
    Case(
        "builtins_dict_mutation_after_load_global_warmup",
        _body(
            f"""
            src = '''
            def f():
                return len([0])
            '''

            builtins = {{"len": lambda obj: 3}}
            ns = {{"__builtins__": builtins}}
            exec(compile(src, "<generated>", "exec"), ns)
            for _ in range({WARMUP}):
                ns["f"]()

            builtins["len"] = lambda obj: 4
            print(json.dumps({{"result": [ns["f"]() for _ in range(8)]}}))
            """
        ),
        [4] * 8,
    ),
    Case(
        "global_function_rebind_after_call_warmup",
        _body(
            f"""
            def g():
                return 1

            def f():
                return g()

            for _ in range({WARMUP}):
                f()

            g = lambda: 2
            print(json.dumps({{"result": [f() for _ in range(8)]}}))
            """
        ),
        [2] * 8,
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
        [160] * 8,
    ),
    Case(
        "function_code_replacement_after_call_warmup",
        _body(
            f"""
            def g():
                return 1

            def h():
                return 2

            def f():
                return g()

            for _ in range({WARMUP}):
                f()

            g.__code__ = h.__code__
            print(json.dumps({{"result": [f() for _ in range(8)]}}))
            """
        ),
        [2] * 8,
    ),
    Case(
        "function_defaults_replacement_after_call_warmup",
        _body(
            f"""
            def g(x=1):
                return x

            def f():
                return g()

            for _ in range({WARMUP}):
                f()

            g.__defaults__ = (2,)
            print(json.dumps({{"result": [f() for _ in range(8)]}}))
            """
        ),
        [2] * 8,
    ),
    Case(
        "real_builtins_len_rebind_after_call_len_warmup",
        _body(
            f"""
            import builtins

            def f():
                return len([1, 2, 3])

            for _ in range({WARMUP}):
                f()

            original_len = builtins.len
            try:
                builtins.len = lambda obj: 7
                result = [f() for _ in range(8)]
            finally:
                builtins.len = original_len

            print(json.dumps({{"result": result}}))
            """
        ),
        [7] * 8,
    ),
    Case(
        "try_finally_continue_break_after_warmup",
        _body(
            f"""
            def f():
                out = []
                for i in range(4):
                    try:
                        if i == 1:
                            continue
                        if i == 2:
                            break
                        out.append(("body", i))
                    finally:
                        out.append(("finally", i))
                return out

            for _ in range({WARMUP}):
                f()

            print(json.dumps({{"result": f()}}))
            """
        ),
        [["body", 0], ["finally", 0], ["finally", 1], ["finally", 2]],
    ),
    Case(
        "nested_exception_finally_control_flow_after_warmup",
        _body(
            f"""
            def f(flag):
                out = []
                try:
                    try:
                        if flag:
                            raise ValueError("sentinel")
                        out.append("body")
                    except ValueError as exc:
                        out.append(type(exc).__name__)
                    finally:
                        out.append("inner-finally")
                finally:
                    out.append("outer-finally")
                return out

            for _ in range({WARMUP}):
                f(True)

            print(json.dumps({{"result": f(True)}}))
            """
        ),
        ["ValueError", "inner-finally", "outer-finally"],
    ),
    Case(
        "compile_optimize_equivalence_without_asserts_or_docstrings",
        _body(
            """
            PROGRAMS = [
                '''
            result = []
            for i in range(5):
                result.append((i, i * i, i % 2 == 0))
            ''',
                '''
            def make(base):
                def f(delta):
                    return base + delta
                return f
            result = [make(i)(10) for i in range(4)]
            ''',
                '''
            result = []
            try:
                raise KeyError("k")
            except KeyError as exc:
                result.append(type(exc).__name__)
            finally:
                result.append("done")
            ''',
                '''
            class C:
                x = 4
                def f(self):
                    return self.x + 1
            result = C().f()
            ''',
                '''
            result = []
            match {"kind": "point", "x": 2, "y": 3}:
                case {"kind": "point", "x": x, "y": y}:
                    result.append(x + y)
                case _:
                    result.append("miss")
            ''',
            ]

            def run(src, optimize):
                ns = {}
                code = compile(src, f"<optimize-{optimize}>", "exec", optimize=optimize)
                exec(code, ns)
                return ns["result"]

            failures = []
            for index, src in enumerate(PROGRAMS):
                values = [run(src, optimize) for optimize in (0, 1, 2)]
                if values[1:] != values[:1] * 2:
                    failures.append({"index": index, "values": values})

            if failures:
                print(json.dumps({"result": failures}))
                raise SystemExit(1)

            print(json.dumps({"result": "ok"}))
            """
        ),
        "ok",
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


def _json_result(proc: subprocess.CompletedProcess[str]) -> object:
    if proc.returncode != 0:
        return None
    lines = proc.stdout.strip().splitlines()
    if not lines:
        return None
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or "result" not in payload:
        return None
    return payload["result"]


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
    expectation_failures: list[dict[str, object]] = []
    for case in selected:
        runs = {mode: _run_case(case, env) for mode, env in modes.items()}
        results = {mode: _normalize(proc) for mode, proc in runs.items()}
        print(f"CASE {case.name}")
        print(json.dumps(results, sort_keys=True))
        baseline = results["tier1"]
        for mode, result in results.items():
            if mode != "tier1" and result != baseline:
                mismatches.append({"case": case.name, "mode": mode, "baseline": baseline, "result": result})
        if case.expected is not None:
            for mode, proc in runs.items():
                observed = _json_result(proc)
                if observed != case.expected:
                    expectation_failures.append(
                        {
                            "case": case.name,
                            "mode": mode,
                            "expected": case.expected,
                            "observed": observed,
                            "raw": results[mode],
                        }
                    )

    if mismatches or expectation_failures:
        if mismatches:
            print("MISMATCHES")
            print(json.dumps(mismatches, indent=2, sort_keys=True))
        if expectation_failures:
            print("EXPECTATION_FAILURES")
            print(json.dumps(expectation_failures, indent=2, sort_keys=True))
        return 1
    print("no mismatches")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
