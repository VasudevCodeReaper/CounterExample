"""Invariant and metamorphic falsification without a reference implementation.

Differential testing is the strongest mode when a trustworthy oracle exists.
This module covers the important complementary case: code with no practical
oracle, but with a testable contract.  A property receives the candidate's
result and the original generated input and returns whether that one execution
satisfies an invariant.  It can also call the candidate again on transformed
inputs to express metamorphic relations such as permutation invariance or
idempotence.

Property checks are deliberately reported as *evidence for the supplied
property*, never as proof of full behavioural equivalence.
"""

from __future__ import annotations

import ast
from typing import Any

from counterexample.adapters import WorkerError, make_implementation
from counterexample.engine.arena import run_differential_arena
from counterexample.generators import SpecError

_WRAPPER = "__counterexample_property"


def _error(message: str, **extra: Any) -> dict[str, Any]:
    return {"status": "ERROR", "error": message, **extra}


def _top_level_callable(code: str, name: str, label: str) -> dict[str, Any] | None:
    """Validate that ``name`` is a top-level sync or async function."""

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return _error(
            f"{label} has a syntax error at line {exc.lineno}: {exc.msg}",
            phase="load",
            details={"exc": "SyntaxError", "line": exc.lineno, "msg": exc.msg},
        )
    if not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name for node in tree.body):
        return _error(f"{label} must define a top-level function named {name!r}", phase="load")
    return None


def _candidate_call(entrypoint: str) -> str:
    if "." not in entrypoint:
        return f"{entrypoint}(*args, **kwargs)"
    cls, method = entrypoint.split(".", 1)
    return f"{cls}().{method}(*args, **kwargs)"


def _uses_candidate_as_a_transformation(property_code: str, entrypoint: str) -> bool:
    """Recognize the common direct-call form of a metamorphic relation."""

    tree = ast.parse(property_code)
    if "." not in entrypoint:
        return any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == entrypoint for node in ast.walk(tree))
    cls, method = entrypoint.split(".", 1)
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == method
        and isinstance(node.func.value, ast.Call)
        and isinstance(node.func.value.func, ast.Name)
        and node.func.value.func.id == cls
        for node in ast.walk(tree)
    )


def _compose_candidate(candidate_code: str, candidate_entrypoint: str, property_code: str, property_entrypoint: str) -> str:
    """Append a wrapper that turns a property violation into ``False``.

    The wrapper is async so it can await either synchronous or asynchronous
    candidates and properties.  It copies arguments before invoking the
    candidate so a property describes the original generated input even when
    the target mutates its arguments in place.
    """

    call = _candidate_call(candidate_entrypoint)
    return (
        candidate_code.rstrip()
        + "\n\n"
        + property_code.rstrip()
        + "\n\n"
        + "async def __counterexample_property(*args, **kwargs):\n"
        + "    _copy = __import__('copy')\n"
        + "    _inspect = __import__('inspect')\n"
        + "    _original_args = _copy.deepcopy(args)\n"
        + "    _original_kwargs = _copy.deepcopy(kwargs)\n"
        + f"    _result = {call}\n"
        + "    if _inspect.isawaitable(_result):\n"
        + "        _result = await _result\n"
        + f"    _verdict = {property_entrypoint}(_result, *_original_args, **_original_kwargs)\n"
        + "    if _inspect.isawaitable(_verdict):\n"
        + "        _verdict = await _verdict\n"
        + "    return bool(_verdict)\n"
    )


def run_property_arena(
    candidate_code: str,
    property_code: str,
    generator_spec: Any = None,
    num_runs: int | None = None,
    timeout_ms: int | None = None,
    *,
    candidate_entrypoint: str | None = None,
    property_entrypoint: str = "property",
    seed: int = 0,
    max_failures: int = 1000,
    workers: int | None = None,
) -> dict[str, Any]:
    """Falsify a candidate against a property or metamorphic relation.

    ``property_code`` defines ``property(result, *args, **kwargs) -> bool``.
    The function may be ``async`` and may call symbols from ``candidate_code``
    to express a metamorphic relation.  For example, an order-independent
    function can check ``result == solve(sorted(values))``.
    """

    prop_error = _top_level_callable(property_code, property_entrypoint, "property_code")
    if prop_error:
        return prop_error
    try:
        candidate = make_implementation(candidate_code, role="candidate", language="python", entrypoint=candidate_entrypoint)
    except WorkerError as exc:
        return _error(str(exc), phase="load", details=exc.info)
    if candidate.language != "python":  # defensive: language is forced above
        return _error("property testing currently supports Python candidates only", phase="load")
    if candidate.entrypoint == _WRAPPER:
        return _error(f"candidate entrypoint {_WRAPPER!r} is reserved", phase="load")
    if generator_spec in (None, "", {}):
        try:
            generator_spec = candidate.infer_spec()
        except (SpecError, NotImplementedError, WorkerError) as exc:
            return _error(f"generator_spec is required: {exc}", phase="spec")

    oracle_code = f"def {_WRAPPER}(*args, **kwargs):\n    return True\n"
    composed = _compose_candidate(candidate_code, candidate.entrypoint, property_code, property_entrypoint)
    result = run_differential_arena(
        oracle_code,
        composed,
        generator_spec,
        num_runs,
        timeout_ms,
        oracle_entrypoint=_WRAPPER,
        candidate_entrypoint=_WRAPPER,
        language="python",
        comparator="exact",
        oracle_errors="fail",
        seed=seed,
        max_failures=max_failures,
        workers=workers,
    )
    if result.get("status") == "ERROR":
        return result

    payload = result.pop("counterexample_payload", None)
    for key in ("shrunk_counterexample", "original_counterexample"):
        item = result.get(key)
        if isinstance(item, dict) and isinstance(item.get("call"), str):
            item["call"] = item["call"].replace(f"{_WRAPPER}(", f"{candidate.entrypoint}(", 1)
    if isinstance(result.get("candidate"), dict):
        result["candidate"]["entrypoint"] = candidate.entrypoint
    result.pop("repro", None)
    if isinstance(result.get("summary"), str):
        result["summary"] = result["summary"].replace(_WRAPPER, candidate.entrypoint)
    result["mode"] = "property"
    result["property"] = {
        "entrypoint": property_entrypoint,
        "candidate_entrypoint": candidate.entrypoint,
        "kind": "metamorphic" if _uses_candidate_as_a_transformation(property_code, candidate.entrypoint) else "invariant",
    }
    if payload:
        result["property_counterexample"] = {
            "schema": "counterexample-property/v1",
            "candidate_entrypoint": candidate.entrypoint,
            "property_entrypoint": property_entrypoint,
            "property_code": property_code,
            "args": payload["args"],
            "kwargs": payload["kwargs"],
        }
    if result["status"] == "VERIFIED":
        result["summary"] = (
            f"PROPERTY VERIFIED: the supplied {result['property']['kind']} property held on "
            f"{result['runs']['valid']}/{result['runs']['valid']} valid adversarial inputs. "
            "This supports the property; it is not a full correctness proof."
        )
        result.setdefault("warnings", []).append(
            "Property verification checks only the supplied invariant. Use run_differential_arena with an independent oracle when one is available."
        )
        result["next_steps"] = [
            "Add an independent oracle when feasible, then run differential testing over the same generator.",
            "Use a different seed and broaden the generator before treating the property as strong evidence.",
        ]
    elif result["status"] == "FALSIFIED":
        result["summary"] = "PROPERTY FALSIFIED: " + result["summary"].removeprefix("FALSIFIED: ")
        result["next_steps"] = [
            "Inspect property_counterexample and candidate_trace, then fix the violated contract.",
            "Turn the shrunk input and property into a permanent regression test in the target project.",
        ]
    return result


__all__ = ["run_property_arena"]
