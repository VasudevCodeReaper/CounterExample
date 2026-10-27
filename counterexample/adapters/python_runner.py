"""Python execution harness.

Code under test is exec'd inside ``_python_worker.py`` running as
``python -I -X utf8`` in a throwaway working directory, one process per
implementation.  Entry points are resolved by name, by convention (``oracle``,
``brute_force``, ``candidate``, ``solve``...), LeetCode-style ``Solution``
classes, or as the last top-level function in the snippet.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any

from counterexample.adapters.base import ProcessSide, WorkerError, sandbox_env
from counterexample.config import get_settings
from counterexample.generators import spec_from_python

WORKER_PATH = Path(__file__).with_name("_python_worker.py")

ROLE_PREFERENCES = {
    "oracle": ["oracle", "brute_force", "bruteforce", "reference", "naive", "solve", "solution"],
    "candidate": ["candidate", "optimized", "fast", "solve", "solution"],
}


def resolve_entrypoint(code: str, role: str, explicit: str | None = None) -> str:
    """Static (AST-only) version of the worker's entrypoint resolution."""
    if explicit:
        return explicit
    tree = ast.parse(code)
    funcs = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("_")]
    for name in ROLE_PREFERENCES.get(role, []):
        if name in funcs:
            return name
    if len(funcs) == 1:
        return funcs[0]
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Solution":
            methods = [m.name for m in node.body if isinstance(m, ast.FunctionDef) and not m.name.startswith("_")]
            if methods:
                return f"Solution.{methods[0]}"
    if funcs:
        return funcs[-1]
    raise WorkerError(f"no function found in {role} code; pass an explicit entrypoint")


class PythonImplementation:
    language = "python"

    def __init__(self, code: str, *, role: str, entrypoint: str | None = None, mode: str = "function"):
        self.code = code
        self.role = role
        self.mode = mode
        self.filename = f"<{role}>"
        self.settings = get_settings()
        if mode == "function":
            try:
                ast.parse(code)
            except SyntaxError as exc:
                raise WorkerError(
                    f"{role} code has a syntax error at line {exc.lineno}: {exc.msg}",
                    {"exc": "SyntaxError", "line": exc.lineno, "msg": exc.msg},
                ) from exc
            self.entrypoint = resolve_entrypoint(code, role, entrypoint)
        else:
            self.entrypoint = entrypoint or "app"

    def prepare(self) -> None:
        """Nothing to compile for Python; syntax was checked in ``__init__``."""

    def infer_spec(self) -> dict[str, Any]:
        name = self.entrypoint.split(".")[-1]
        return spec_from_python(self.code, name)

    def spawn(self, timeout_s: float) -> ProcessSide:
        setup = {
            "op": "setup",
            "code": self.code,
            "entrypoint": self.entrypoint,
            "role": self.role,
            "mode": self.mode,
            "filename": self.filename,
        }
        env = sandbox_env({"CX_MEMORY_MB": str(self.settings.sandbox_memory_mb)})
        return ProcessSide(
            [sys.executable, "-I", "-X", "utf8", str(WORKER_PATH)],
            label=self.role,
            setup=setup,
            timeout_s=timeout_s,
            env=env,
        )

    def describe(self) -> dict[str, Any]:
        return {"language": self.language, "entrypoint": self.entrypoint, "mode": self.mode}

    @property
    def supports_trace(self) -> bool:
        return self.mode == "function"
