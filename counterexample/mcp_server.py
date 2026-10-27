"""FastMCP agent bridge: exposes the Counterexample engine as MCP tools.

Run with ``python -m counterexample.mcp_server`` (stdio, for Claude Code /
Claude Desktop / Cursor / Windsurf) or over HTTP for remote agents::

    python -m counterexample.mcp_server --transport streamable-http --host 0.0.0.0 --port 8000

Works with both ``mcp`` 1.x (``FastMCP``) and ``mcp`` 2.x (``MCPServer``).
"""

from __future__ import annotations

import argparse
import functools
import hmac
import os
import sys
from typing import Any

import anyio

from counterexample import __version__
from counterexample.engine import arena, git_oracle, profiler, property as property_engine, regression
from counterexample.generators import GENERATOR_REFERENCE
from counterexample.resources import skill_text

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server

    MCP_V2 = True
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]

    MCP_V2 = False

INSTRUCTIONS = """\
Counterexample replaces "all unit tests passed" with empirical falsification.
Use it whenever you refactor existing code, replace a brute-force algorithm with an
optimized one, or write logic over intervals, graphs, trees, DP, parsers or boundary
arithmetic.

Self-play loop:
1. Oracle: refactor -> extract_git_oracle(file_path, symbol_name, git_ref="HEAD");
   new optimized code -> write a dead-simple brute-force oracle (<= 20 obvious lines).
2. Generator: pass a generator_spec (read resource counterexample://generators) or omit it
   to infer from type hints / C++ signatures. Boundary values are included by default.
   Keep N <= 50 against exponential or O(N^2) oracles.
3. run_differential_arena(oracle_code, candidate_code, generator_spec). If FALSIFIED, do
   not guess: trace the candidate on shrunk_counterexample, find the diverging line, patch,
   re-run with the same seed until VERIFIED.
4. profile_empirical_complexity(candidate_code, ...) to catch silent O(N^2) regressions.
5. When an oracle is unavailable, use run_property_arena with a strong independent invariant or
   metamorphic relation. A property verdict supports only that stated contract; it is not a full
   correctness proof.
6. lock_regression_test(counterexample_payload, test_file_path, framework) for every differential
   counterexample found.
"""

mcp = _Server(name="counterexample", instructions=INSTRUCTIONS)


async def _offload(fn: Any, /, **kwargs: Any) -> Any:
    # Engine calls block on subprocess pipes; keep the event loop responsive.
    return await anyio.to_thread.run_sync(functools.partial(fn, **kwargs))


@mcp.tool()
async def run_differential_arena(
    oracle_code: str,
    candidate_code: str,
    generator_spec: Any = None,
    num_runs: int | None = None,
    timeout_ms: int | None = None,
    oracle_entrypoint: str | None = None,
    candidate_entrypoint: str | None = None,
    language: str = "auto",
    oracle_language: str | None = None,
    candidate_language: str | None = None,
    comparator: str = "auto",
    float_tolerance: float = 1e-9,
    compare_inputs: str = "auto",
    oracle_errors: str = "discard",
    seed: int = 0,
    max_failures: int = 1000,
) -> dict[str, Any]:
    """Falsify candidate_code against a ground-truth oracle_code on thousands of adversarial inputs.

    Returns status VERIFIED (pass rate + speedup multiplier such as "24.6x faster"),
    FALSIFIED (the delta-shrunk minimal counterexample, exact oracle-vs-candidate diff,
    a line trace of the candidate on that input, and counterexample_payload for
    lock_regression_test), INCONCLUSIVE or ERROR.

    - oracle_code / candidate_code: Python source (a function, or a `class Solution` method),
      C++17/20 source, a FastAPI app, or a base URL. language/oracle_language/candidate_language
      may be auto|python|cpp|fastapi - oracle and candidate may use different languages.
    - generator_spec: JSON spec (see resource counterexample://generators), shorthand like
      "list[int(-50,50)]", {"args": [...]} for several arguments, or omit to infer from type hints.
    - num_runs: defaults to $DEFAULT_ARENA_RUNS (5000). timeout_ms: per call, default $SANDBOX_TIMEOUT_MS.
    - comparator: auto (float tolerant) | exact | strict | approx | unordered | unordered_deep | set.
    - compare_inputs: auto (compare mutated arguments when both return None) | always | never.
    - oracle_errors: discard (inputs the oracle rejects are skipped) | fail (exception behaviour is
      part of the contract, e.g. refactors).
    """
    return await _offload(
        arena.run_differential_arena,
        oracle_code=oracle_code,
        candidate_code=candidate_code,
        generator_spec=generator_spec,
        num_runs=num_runs,
        timeout_ms=timeout_ms,
        oracle_entrypoint=oracle_entrypoint,
        candidate_entrypoint=candidate_entrypoint,
        language=language,
        oracle_language=oracle_language,
        candidate_language=candidate_language,
        comparator=comparator,
        float_tolerance=float_tolerance,
        compare_inputs=compare_inputs,
        oracle_errors=oracle_errors,
        seed=seed,
        max_failures=max_failures,
    )


@mcp.tool()
async def run_property_arena(
    candidate_code: str,
    property_code: str,
    generator_spec: Any = None,
    num_runs: int | None = None,
    timeout_ms: int | None = None,
    candidate_entrypoint: str | None = None,
    property_entrypoint: str = "property",
    seed: int = 0,
    max_failures: int = 1000,
) -> dict[str, Any]:
    """Falsify a Python invariant or metamorphic relation when no oracle exists.

    property_code must define ``property(result, *args, **kwargs) -> bool``. It can be async and
    may call symbols from candidate_code on transformed inputs, for example
    ``return result == solve(sorted(values))``. A VERIFIED result means this one supplied
    property held; it is not a proof that the candidate matches an unspecified reference.
    """
    return await _offload(
        property_engine.run_property_arena,
        candidate_code=candidate_code,
        property_code=property_code,
        generator_spec=generator_spec,
        num_runs=num_runs,
        timeout_ms=timeout_ms,
        candidate_entrypoint=candidate_entrypoint,
        property_entrypoint=property_entrypoint,
        seed=seed,
        max_failures=max_failures,
    )


@mcp.tool()
async def extract_git_oracle(
    file_path: str,
    symbol_name: str,
    git_ref: str = "HEAD",
    repo_path: str | None = None,
    include_dependencies: bool = True,
) -> dict[str, Any]:
    """Extract the pre-refactor implementation of a function/method from git (HEAD or any commit)
    to act as a zero-effort ground-truth oracle.

    Returns oracle_code (the symbol plus the imports, helpers, constants and classes it depends on),
    the oracle entrypoint, the commit, and whether/how the working-tree version differs.
    symbol_name may be "func", "Class.method" (Python) or "ns::func" / "Class::method" (C++).
    Relative file paths are resolved against repo_path (default: the server's working directory).
    """
    return await _offload(
        git_oracle.extract_git_oracle,
        file_path=file_path,
        symbol_name=symbol_name,
        git_ref=git_ref,
        repo_path=repo_path,
        include_dependencies=include_dependencies,
    )


@mcp.tool()
async def profile_empirical_complexity(
    candidate_code: str,
    generator_spec: Any = None,
    scale_steps: list[int] | None = None,
    entrypoint: str | None = None,
    language: str = "auto",
    expected_complexity: str | None = None,
    repeats: int = 5,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    """Run the candidate at N = 10, 100, 1000, 10000 (or scale_steps), fit execution-time and
    memory growth curves (O(1) ... O(2^N)) and flag silent O(N^2) regressions before production.

    Collections in generator_spec are scaled to exactly N elements (matrices to ~N cells);
    mark an int argument with "scale": true to set it to N. Pass expected_complexity
    (e.g. "O(N log N)") to get a REGRESSION flag when the measured growth is worse.
    """
    return await _offload(
        profiler.profile_empirical_complexity,
        candidate_code=candidate_code,
        generator_spec=generator_spec,
        scale_steps=scale_steps,
        entrypoint=entrypoint,
        language=language,
        expected_complexity=expected_complexity,
        repeats=repeats,
        timeout_ms=timeout_ms,
    )


@mcp.tool()
async def lock_regression_test(
    counterexample_payload: Any,
    test_file_path: str,
    framework: str = "pytest",
    module: str | None = None,
    import_line: str | None = None,
    function_name: str | None = None,
) -> dict[str, Any]:
    """Convert a captured minimal counterexample into a permanent pytest, jest (or vitest) or
    gtest unit test appended to test_file_path (created if missing). Idempotent.

    counterexample_payload is the object returned by run_differential_arena. module is the
    import path (pytest: "pkg.mod"; jest: "./src/mod"; gtest: header path); when omitted the
    project is searched for the definition.
    """
    return await _offload(
        regression.lock_regression_test,
        counterexample_payload=counterexample_payload,
        test_file_path=test_file_path,
        framework=framework,
        module=module,
        import_line=import_line,
        function_name=function_name,
    )


@mcp.resource("counterexample://skill", name="skill", description="The portable Counterexample agent skill (SKILL.md).", mime_type="text/markdown")
def skill_resource() -> str:
    return skill_text()


@mcp.resource("counterexample://generators", name="generators", description="generator_spec reference.", mime_type="text/markdown")
def generators_resource() -> str:
    return GENERATOR_REFERENCE


@mcp.prompt(name="falsify", description="Run the Counterexample self-play loop on a function.")
def falsify_prompt(target: str, strategy: str = "auto") -> str:
    return (
        f"Use the Counterexample falsification protocol on {target}.\n"
        f"Oracle strategy: {strategy} (refactor -> extract_git_oracle; new optimized code -> brute-force oracle).\n\n"
        + INSTRUCTIONS
    )


_LOOPBACK = ("127.0.0.1", "localhost", "::1")


class BearerAuth:
    """ASGI middleware: require ``Authorization: Bearer <token>`` on every HTTP
    request except ``GET /healthz``. The server executes submitted code, so a
    network-reachable instance must never be anonymous."""

    def __init__(self, app: Any, token: str | None):
        self.app = app
        self.token = token.encode() if token else None

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("path") == "/healthz":
            await self._respond(send, 200, b'{"status":"ok"}')
            return
        if self.token is not None:
            headers = dict(scope.get("headers") or [])
            supplied = headers.get(b"authorization", b"")
            if not hmac.compare_digest(supplied, b"Bearer " + self.token):
                await self._respond(send, 401, b'{"error":"missing or invalid bearer token"}', [(b"www-authenticate", b"Bearer")])
                return
        await self.app(scope, receive, send)

    @staticmethod
    async def _respond(send: Any, status: int, body: bytes, extra: list[tuple[bytes, bytes]] | None = None) -> None:
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())] + (extra or [])
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


def http_app(transport: str, host: str, token: str | None) -> Any:
    """Build the ASGI app for an HTTP transport, wrapped with bearer auth."""
    kwargs: dict[str, Any] = {}
    if MCP_V2:
        kwargs["host"] = host
        allowed = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
        if allowed:
            from mcp.server.transport_security import TransportSecuritySettings

            kwargs["transport_security"] = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=allowed)
    app = mcp.streamable_http_app(**kwargs) if transport == "streamable-http" else mcp.sse_app(**kwargs)
    return BearerAuth(app, token)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="counterexample-mcp", description="Counterexample MCP server")
    parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default=os.environ.get("MCP_TRANSPORT", "stdio"))
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--version", action="version", version=f"counterexample {__version__}")
    args = parser.parse_args(argv)
    if args.transport == "stdio":
        mcp.run("stdio")
        return
    token = os.environ.get("COUNTEREXAMPLE_API_TOKEN") or None
    if token is None and args.host not in _LOOPBACK and os.environ.get("COUNTEREXAMPLE_ALLOW_UNAUTHENTICATED") != "1":
        sys.exit(
            f"refusing to serve on {args.host} without authentication: this server executes submitted code.\n"
            "Set COUNTEREXAMPLE_API_TOKEN (clients send 'Authorization: Bearer <token>'), bind to 127.0.0.1, "
            "or set COUNTEREXAMPLE_ALLOW_UNAUTHENTICATED=1 inside an isolated network."
        )
    import uvicorn

    uvicorn.run(http_app(args.transport, args.host, token), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
