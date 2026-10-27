import pytest
from conftest import requires_fastapi

from counterexample import run_differential_arena

pytestmark = [pytest.mark.fastapi, requires_fastapi]

LEGACY = '''
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

class Body(BaseModel):
    intervals: list[list[int]]

@app.post("/merge")
def merge(body: Body):
    out = []
    for lo, hi in sorted(body.intervals):
        if out and lo <= out[-1][1]:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return {"merged": out}
'''

REWRITE = LEGACY.replace("if out and lo <= out[-1][1]:", "if out and lo < out[-1][1]:")

SPEC = {
    "type": "http_request",
    "method": "POST",
    "path": "/merge",
    "json": {"type": "dict", "keys": {"type": "const", "value": "intervals"}, "values": {"type": "intervals", "count": [0, 8]}, "min_size": 1, "max_size": 1},
}


def test_endpoint_migration_falsified():
    r = run_differential_arena(LEGACY, REWRITE, SPEC, num_runs=300)
    assert r["status"] == "FALSIFIED", r.get("error")
    assert r["oracle"]["language"] == "fastapi"
    # Minimal input: two touching intervals.
    assert r["shrunk_counterexample"]["call"].count("[") >= 3
    assert "merged" in r["oracle_output"]


def test_endpoint_identical_verified():
    r = run_differential_arena(LEGACY, LEGACY, SPEC, num_runs=150)
    assert r["status"] == "VERIFIED"
