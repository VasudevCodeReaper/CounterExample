import json

import anyio

from counterexample.mcp_server import mcp


def _schema(tool):
    return getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {}


def _payload(result):
    content = getattr(result, "content", None)
    if content is None and isinstance(result, tuple):  # mcp 1.x returns (content, structured)
        content = result[0]
    if isinstance(result, list):
        content = result
    return json.loads(content[0].text)


def test_tools_are_registered():
    tools = anyio.run(mcp.list_tools)
    names = {t.name for t in tools}
    assert names == {"run_differential_arena", "extract_git_oracle", "profile_empirical_complexity", "lock_regression_test"}
    arena = next(t for t in tools if t.name == "run_differential_arena")
    props = _schema(arena)["properties"]
    for key in ("oracle_code", "candidate_code", "generator_spec", "num_runs", "timeout_ms"):
        assert key in props
    assert {"oracle_code", "candidate_code"} <= set(_schema(arena)["required"])


def test_resources_are_registered():
    resources = anyio.run(mcp.list_resources)
    assert {str(r.uri) for r in resources} == {"counterexample://skill", "counterexample://generators"}


def test_call_arena_tool():
    async def go():
        return await mcp.call_tool(
            "run_differential_arena",
            {
                "oracle_code": "def f(a: list[int]) -> int:\n    return max(a) if a else 0\n",
                "candidate_code": "def f(a: list[int]) -> int:\n    return sorted(a)[-1] if a else 1\n",
                "num_runs": 200,
            },
        )

    data = _payload(anyio.run(go))
    assert data["status"] == "FALSIFIED"
    assert data["shrunk_counterexample"]["call"] == "f([])"


def test_bearer_auth_middleware():
    import pytest

    testclient = pytest.importorskip("starlette.testclient")
    from counterexample.mcp_server import BearerAuth

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"inner"})

    client = testclient.TestClient(BearerAuth(inner, "tok"))
    assert client.get("/healthz").status_code == 200
    assert client.post("/mcp").status_code == 401
    assert client.post("/mcp", headers={"Authorization": "Bearer nope"}).status_code == 401
    ok = client.post("/mcp", headers={"Authorization": "Bearer tok"})
    assert ok.status_code == 200 and ok.text == "inner"


def test_refuses_public_bind_without_token(monkeypatch):
    import pytest

    from counterexample.mcp_server import main

    monkeypatch.delenv("COUNTEREXAMPLE_API_TOKEN", raising=False)
    monkeypatch.delenv("COUNTEREXAMPLE_ALLOW_UNAUTHENTICATED", raising=False)
    with pytest.raises(SystemExit) as exc:
        main(["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "0"])
    assert "refusing to serve" in str(exc.value)
