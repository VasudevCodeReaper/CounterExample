"""Deterministic delta-debugging reducer.

Given a failing case, the shrinker repeatedly asks the case generator for
simpler variants (ddmin chunk removal first - halves, quarters, ... singles -
then structure-specific reductions such as dropping graph nodes, translating
intervals toward the origin, and binary-searching numbers toward zero) and
keeps the first variant that still fails *in the same way*.

Termination is guaranteed: a variant is only accepted when it is strictly
smaller under the well-founded order ``(atoms, magnitude, literal length,
literal text)``, and the evaluation budget is capped.  The result is a local
minimum - removing any single element or simplifying any single value makes
the failure disappear - which is what lets an LLM trace it step by step.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from counterexample.codec import py_literal, value_size
from counterexample.generators.base import CaseGenerator

# evaluate(candidates) -> index of the first candidate that still fails (and
# the failure payload), or None when every candidate passes.
BatchEvaluator = Callable[[list[dict[str, Any]]], "tuple[int, Any] | None"]


def _magnitude(v: Any, depth: int = 0) -> float:
    if depth > 50:
        return 0.0
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, int):
        return float(min(abs(v), 2**63))
    if isinstance(v, float):
        return 1e18 if (math.isnan(v) or math.isinf(v)) else min(abs(v), 1e18)
    if isinstance(v, str):
        return float(sum(min(ord(ch), 0x10FFFF) > 127 for ch in v))
    if isinstance(v, dict):
        return sum(_magnitude(k, depth + 1) + _magnitude(x, depth + 1) for k, x in v.items())
    if isinstance(v, (list, tuple, set, frozenset)):
        return sum(_magnitude(x, depth + 1) for x in v)
    return 0.0


def complexity(case_gen: CaseGenerator, case: dict[str, Any]) -> tuple[int, float, int, str]:
    args, kwargs = case_gen.materialize(case)
    text = py_literal(args) + py_literal(kwargs)
    return (value_size(args) + value_size(kwargs), _magnitude(args) + _magnitude(kwargs), len(text), text)


@dataclass
class ShrinkResult:
    case: dict[str, Any]
    failure: Any
    original_size: int
    shrunk_size: int
    steps: int
    evaluations: int
    elapsed_s: float
    local_minimum: bool
    size_history: list[int] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "original_size": self.original_size,
            "shrunk_size": self.shrunk_size,
            "steps": self.steps,
            "evaluations": self.evaluations,
            "elapsed_ms": round(self.elapsed_s * 1000, 1),
            "local_minimum": self.local_minimum,
            "size_history": self.size_history[:50],
        }


class DeltaShrinker:
    def __init__(
        self,
        case_gen: CaseGenerator,
        evaluate: BatchEvaluator,
        *,
        max_evals: int = 5000,
        budget_s: float = 30.0,
        batch_size: int = 8,
    ):
        self.case_gen = case_gen
        self.evaluate = evaluate
        self.max_evals = max_evals
        self.budget_s = budget_s
        self.batch_size = max(1, batch_size)

    def _candidates(self, current: dict[str, Any], bound: tuple[Any, ...], seen: set[str]) -> Iterator[tuple[dict[str, Any], str]]:
        for cand in self.case_gen.shrink(current):
            key = complexity(self.case_gen, cand)
            if key >= bound or key[3] in seen:
                continue
            seen.add(key[3])
            yield cand, key[3]

    def shrink(self, case: dict[str, Any], failure: Any) -> ShrinkResult:
        start = time.monotonic()
        current, cur_failure = case, failure
        bound = complexity(self.case_gen, current)
        original = bound[0]
        history = [original]
        seen: set[str] = {bound[3]}
        evals = steps = 0
        local_min = False
        while evals < self.max_evals and time.monotonic() - start < self.budget_s:
            hit = None
            batch: list[tuple[dict[str, Any], str]] = []
            for item in self._candidates(current, bound, seen):
                batch.append(item)
                if len(batch) >= self.batch_size:
                    evals += len(batch)
                    hit = self._probe(batch, seen)
                    batch = []
                    if hit is not None or evals >= self.max_evals or time.monotonic() - start >= self.budget_s:
                        break
            if hit is None and batch and evals < self.max_evals:
                evals += len(batch)
                hit = self._probe(batch, seen)
            if hit is None:
                local_min = evals < self.max_evals and time.monotonic() - start < self.budget_s
                break
            current, cur_failure = hit
            bound = complexity(self.case_gen, current)
            steps += 1
            history.append(bound[0])
        return ShrinkResult(
            case=current,
            failure=cur_failure,
            original_size=original,
            shrunk_size=bound[0],
            steps=steps,
            evaluations=evals,
            elapsed_s=time.monotonic() - start,
            local_minimum=local_min,
            size_history=history,
        )

    def _probe(self, batch: list[tuple[dict[str, Any], str]], seen: set[str]) -> tuple[dict[str, Any], Any] | None:
        found = self.evaluate([cand for cand, _ in batch])
        if found is None:
            return None
        idx, failure = found
        # Candidates queued after the hit may not have been judged; let them be retried.
        for _, key in batch[idx + 1 :]:
            seen.discard(key)
        return batch[idx][0], failure
