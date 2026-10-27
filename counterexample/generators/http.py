"""HTTP request generator for differential testing of API endpoints.

    {"type": "http_request", "method": "POST", "path": "/items/{item_id}",
     "path_params": {"item_id": {"type": "int", "min": 0, "max": 9}},
     "query": {"q": {"type": "str", "max_len": 5}},
     "json": {"type": "dict", "values": "int"},
     "headers": {"x-tenant": {"type": "choice", "values": ["a", "b"]}}}

Values are request dicts ``{"method", "path", "params", "json", "headers",
"path_params"}`` consumed by the FastAPI adapter.
"""

from __future__ import annotations

import random
from typing import Any, Iterator
from urllib.parse import quote

from counterexample.generators.base import Generator, SpecError, build, register


@register("http_request", "request")
class HttpRequestGen(Generator):
    def __init__(self, spec: dict[str, Any]):
        super().__init__(spec)
        method = spec.get("method", "GET")
        self.methods = [m.upper() for m in (method if isinstance(method, list) else [method])]
        self.path = spec.get("path", "/")
        if not isinstance(self.path, str) or not self.path.startswith("/"):
            raise SpecError("http_request 'path' must start with '/'")
        self.path_params = {k: build(v) for k, v in (spec.get("path_params") or {}).items()}
        self.query = {k: build(v) for k, v in (spec.get("query") or {}).items()}
        self.headers = {k: build(v) for k, v in (spec.get("headers") or {}).items()}
        self.body = build(spec["json"]) if spec.get("json") is not None else None
        self.scalable = bool(self.body and self.body.scalable)

    def _render(self, method: str, path_values: dict[str, Any], query: dict[str, Any], body: Any, headers: dict[str, Any]) -> dict[str, Any]:
        path = self.path
        for k, v in path_values.items():
            path = path.replace("{" + k + "}", quote(str(v), safe=""))
        req: dict[str, Any] = {"method": method, "path": path, "path_params": path_values}
        if query:
            req["params"] = query
        if self.body is not None:
            req["json"] = body
        if headers:
            req["headers"] = {k: str(v) for k, v in headers.items()}
        return req

    def _parts(self, value: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any], Any, dict[str, Any]]:
        return (
            value.get("method", self.methods[0]),
            dict(value.get("path_params") or {}),
            dict(value.get("params") or {}),
            value.get("json"),
            dict(value.get("headers") or {}),
        )

    def generate(self, rng: random.Random, size: float) -> dict[str, Any]:
        return self._render(
            rng.choice(self.methods),
            {k: g.generate(rng, size) for k, g in self.path_params.items()},
            {k: g.generate(rng, size) for k, g in self.query.items()},
            self.body.generate(rng, size) if self.body else None,
            {k: g.generate(rng, size) for k, g in self.headers.items()},
        )

    def scaled(self, rng: random.Random, n: int) -> dict[str, Any]:
        value = self.generate(rng, 1.0)
        if self.body is not None:
            value["json"] = self.body.scaled(rng, n)
        return value

    def edge_cases(self) -> list[dict[str, Any]]:
        def first(g: Generator) -> Any:
            e = g.edge_cases()
            return e[0] if e else g.generate(random.Random(0), 0.0)

        base_path = {k: first(g) for k, g in self.path_params.items()}
        base_query = {k: first(g) for k, g in self.query.items()}
        base_headers = {k: first(g) for k, g in self.headers.items()}
        bodies = (self.body.edge_cases()[:8] or [None]) if self.body else [None]
        return [self._render(m, base_path, base_query, b, base_headers) for m in self.methods for b in bodies]

    def shrink(self, value: Any) -> Iterator[dict[str, Any]]:
        if not isinstance(value, dict):
            return
        method, pv, q, body, hdrs = self._parts(value)
        if method != self.methods[0]:
            yield self._render(self.methods[0], pv, q, body, hdrs)
        if self.body is not None:
            for b in self.body.shrink(body):
                yield self._render(method, pv, q, b, hdrs)
        for k, g in self.path_params.items():
            for c in g.shrink(pv.get(k)):
                yield self._render(method, {**pv, k: c}, q, body, hdrs)
        for k, g in self.query.items():
            for c in g.shrink(q.get(k)):
                yield self._render(method, pv, {**q, k: c}, body, hdrs)
