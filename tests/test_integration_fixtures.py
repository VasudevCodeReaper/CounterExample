"""Integration seams for generated tests against a real local project."""

from __future__ import annotations

from counterexample import run_differential_arena, run_property_arena


def _write_stateful_project(tmp_path):
    (tmp_path / "vibe_app.py").write_text(
        '''
import random
import time

calls = []

def assess(value: int) -> dict[str, float | int]:
    calls.append(value)
    return {"value": value, "call": len(calls), "nonce": random.randint(0, 1_000_000), "clock": time.time()}
''',
        encoding="utf-8",
    )
    wrapper = "from vibe_app import assess\n"
    fixture = '''
import random
import time
import vibe_app

_real_time = time.time

def before_call(args, kwargs):
    vibe_app.calls.clear()
    random.seed(991)
    vibe_app.time.time = lambda: 1_700_000_000.0

def after_call(args, kwargs, result, error):
    vibe_app.time.time = _real_time
'''
    return wrapper, fixture


def test_import_paths_and_fixtures_make_stateful_project_calls_reproducible(tmp_path):
    wrapper, fixture = _write_stateful_project(tmp_path)

    result = run_differential_arena(
        wrapper,
        wrapper,
        "int(-300,300)",
        num_runs=30,
        oracle_entrypoint="assess",
        candidate_entrypoint="assess",
        import_paths=[str(tmp_path)],
        oracle_fixture_code=fixture,
        candidate_fixture_code=fixture,
    )

    assert result["status"] == "VERIFIED", result
    assert result["runs"]["valid"] == 30
    assert result["oracle"]["import_paths"] == [str(tmp_path.resolve())]
    assert result["candidate"]["has_fixture"] is True


def test_property_arena_supports_project_imports_and_per_case_fixtures(tmp_path):
    wrapper, fixture = _write_stateful_project(tmp_path)
    property_code = '''
def property(result, value):
    return result["value"] == value and result["call"] == 1 and result["clock"] == 1_700_000_000.0
'''

    result = run_property_arena(
        wrapper,
        property_code,
        "int(-3,3)",
        num_runs=30,
        candidate_entrypoint="assess",
        import_paths=[str(tmp_path)],
        fixture_code=fixture,
    )

    assert result["status"] == "VERIFIED", result
    assert result["candidate"]["has_fixture"] is True


def test_async_fixture_cleanup_is_awaited_between_generated_calls():
    oracle = "def f(value: int) -> int:\n    return value\n"
    candidate = '''
history = []

def f(value: int) -> int:
    history.append(value)
    return value if len(history) == 1 else -999
'''
    fixture = '''
import asyncio

async def before_call(args, kwargs):
    await asyncio.sleep(0)

async def after_call(args, kwargs, result, error):
    await asyncio.sleep(0)
    history.clear()
'''

    result = run_differential_arena(
        oracle,
        candidate,
        "int(-300,300)",
        num_runs=30,
        candidate_fixture_code=fixture,
    )

    assert result["status"] == "VERIFIED", result


def test_bad_fixture_is_reported_during_load():
    result = run_differential_arena(
        "def f(x: int) -> int:\n    return x\n",
        "def f(x: int) -> int:\n    return x\n",
        "int(0,1)",
        num_runs=2,
        candidate_fixture_code="def before_call(:\n    pass\n",
    )

    assert result["status"] == "ERROR"
    assert result["phase"] == "load"
    assert "fixture_code has a syntax error" in result["error"]
