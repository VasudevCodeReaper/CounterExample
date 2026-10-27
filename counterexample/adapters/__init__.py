"""Execution harnesses: Python functions, C++17/20 functions and FastAPI endpoints."""

from __future__ import annotations

import ast
import re
from typing import Any, Protocol

from counterexample.adapters.base import CallResult, ProcessSide, WorkerError

LANGUAGES = ("python", "cpp", "fastapi")

_ALIASES = {
    "py": "python",
    "python3": "python",
    "c++": "cpp",
    "cxx": "cpp",
    "cc": "cpp",
    "c++17": "cpp",
    "c++20": "cpp",
    "http": "fastapi",
    "api": "fastapi",
}

_CPP_HINTS = re.compile(r"#include\s*[<\"]|\bstd::|\busing\s+namespace\s+std\b|\btemplate\s*<|\bint\s+main\s*\(")


class Implementation(Protocol):
    language: str
    role: str
    entrypoint: str

    def prepare(self) -> None: ...

    def infer_spec(self) -> dict[str, Any]: ...

    def spawn(self, timeout_s: float) -> ProcessSide: ...

    def describe(self) -> dict[str, Any]: ...

    @property
    def supports_trace(self) -> bool: ...


def detect_language(code: str) -> str:
    stripped = code.strip()
    if stripped.startswith(("http://", "https://")):
        return "fastapi"
    if _CPP_HINTS.search(code):
        return "cpp"
    if re.search(r"\bFastAPI\s*\(", code) or re.search(r"\bAPIRouter\s*\(", code):
        return "fastapi"
    try:
        ast.parse(code)
    except SyntaxError:
        # Bare C/C++ (no #include): braces, semicolons and a typed signature.
        if "{" in code and ";" in code and re.search(r"\b[A-Za-z_][\w:<>,\s\*&]*\s+[A-Za-z_]\w*\s*\([^)]*\)\s*(const\s*)?\{", code):
            return "cpp"
    return "python"


def normalize_language(language: str | None, code: str) -> str:
    if not language or language == "auto":
        return detect_language(code)
    lang = _ALIASES.get(language.lower(), language.lower())
    if lang not in LANGUAGES:
        raise WorkerError(f"unsupported language {language!r}; choose from {', '.join(LANGUAGES)} or 'auto'")
    return lang


def make_implementation(
    code: str,
    *,
    role: str,
    language: str | None = "auto",
    entrypoint: str | None = None,
    cxx_std: str | None = None,
    import_paths: list[str] | None = None,
    fixture_code: str | None = None,
) -> Implementation:
    if not isinstance(code, str) or not code.strip():
        raise WorkerError(f"{role}_code is empty")
    lang = normalize_language(language, code)
    if lang == "python":
        from counterexample.adapters.python_runner import PythonImplementation

        return PythonImplementation(code, role=role, entrypoint=entrypoint, import_paths=import_paths, fixture_code=fixture_code)
    if lang == "cpp":
        from counterexample.adapters.cpp_runner import CppImplementation

        return CppImplementation(code, role=role, entrypoint=entrypoint, std=cxx_std)
    from counterexample.adapters.fastapi_runner import FastAPIImplementation

    return FastAPIImplementation(code, role=role, entrypoint=entrypoint, import_paths=import_paths, fixture_code=fixture_code)


__all__ = [
    "CallResult",
    "Implementation",
    "LANGUAGES",
    "ProcessSide",
    "WorkerError",
    "detect_language",
    "make_implementation",
    "normalize_language",
]
