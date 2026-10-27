"""FastAPI / HTTP endpoint harness.

Use it when migrating or refactoring an endpoint whose external behaviour must
stay identical.  Each side is either

* Python source that defines a FastAPI ``app`` (or an app factory, or any name
  passed as the entrypoint) - requests go through ``fastapi.testclient`` inside
  the sandbox worker, no server needed; or
* a base URL (``http://localhost:8000``) of a running service.

Responses are normalized to ``{"status_code": int, "json": ...}`` (or
``"text"`` for non-JSON bodies) and compared like any other output, so a
legacy service can serve as the ground-truth oracle for its rewrite.
"""

from __future__ import annotations

import importlib.util
from typing import Any

from counterexample.adapters.base import WorkerError
from counterexample.adapters.python_runner import PythonImplementation
from counterexample.generators import SpecError


class FastAPIImplementation(PythonImplementation):
    language = "fastapi"

    def __init__(self, code: str, *, role: str, entrypoint: str | None = None):
        is_url = code.strip().startswith(("http://", "https://"))
        if not is_url and importlib.util.find_spec("fastapi") is None:
            raise WorkerError("the fastapi adapter needs `pip install counterexample[fastapi]` (fastapi + httpx)")
        super().__init__(code, role=role, entrypoint=entrypoint, mode="fastapi")

    def infer_spec(self) -> dict[str, Any]:
        raise SpecError(
            'endpoint testing needs an explicit generator_spec, e.g. {"type": "http_request", '
            '"method": "POST", "path": "/merge", "json": {"type": "intervals"}}'
        )

    def describe(self) -> dict[str, Any]:
        return {"language": self.language, "entrypoint": self.entrypoint, "target": "url" if self.code.strip().startswith("http") else "app"}

    @property
    def supports_trace(self) -> bool:
        return False
