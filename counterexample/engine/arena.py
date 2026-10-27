"""Differential arena: pit an optimized candidate against a ground-truth oracle.

Pipeline::

    generator_spec --> boundary cases + ramped random cases (deduplicated, seeded)
                   --> N parallel shards, each a (oracle process, candidate process) pair
                   --> classify every input: pass / fail(kind) / discarded
                   --> delta-shrink the simplest failure to a minimal counterexample
                   --> exact output diff + line trace of the candidate on that input
"""

from __future__ import annotations

import json
import math
import queue
import random
import threading
import time
from dataclasses import dataclass
from typing import Any

from counterexample.adapters import CallResult, Implementation, ProcessSide, WorkerError, make_implementation
from counterexample.codec import encode, py_literal, short_repr
from counterexample.config import get_settings
from counterexample.engine.compare import MODES as COMPARATOR_MODES
from counterexample.engine.compare import make_comparator
from counterexample.engine.diff import structural_diff, summarize, text_diff
from counterexample.engine.shrinker import DeltaShrinker
from counterexample.generators import CaseGenerator, SpecError

PAYLOAD_SCHEMA = "counterexample/v1"

# Lower sorts first when choosing which failure to shrink.
_KIND_PRIORITY = {"mismatch": 0, "unexpected_success": 0, "exception_mismatch": 0, "candidate_crash": 1, "candidate_timeout": 2}


def _kind_priority(kind: str) -> int:
    return 0 if kind.startswith("candidate_exception") else _KIND_PRIORITY.get(kind, 1)


@dataclass
class Outcome:
    verdict: str  # pass | fail | invalid | skipped
    kind: str | None
    oracle: CallResult
    candidate: CallResult
    oracle_view: Any = None
    candidate_view: Any = None


class _Arena:
    def __init__(
        self,
        oracle: Implementation,
        candidate: Implementation,
        case_gen: CaseGenerator,
        *,
        comparator: str,
        float_tolerance: float,
        compare_inputs: str,
        oracle_errors: str,
        timeout_s: float,
        workers: int,
    ):
        self.oracle = oracle
        self.candidate = candidate
        self.case_gen = case_gen
        self.eq = make_comparator(comparator, float_tolerance)
        self.compare_inputs = compare_inputs
        self.oracle_errors = oracle_errors
        self.timeout_s = timeout_s
        self.workers = max(1, workers)
        self.cross_language = oracle.language != candidate.language

    # -- messages & classification ---------------------------------------- #
    def message(self, idx: int, case: dict[str, Any]) -> bytes:
        args, kwargs = self.case_gen.materialize(case)
        msg: dict[str, Any] = {"op": "call", "id": idx, "args": encode(args), "kwargs": encode(kwargs)}
        if self.compare_inputs == "always":
            msg["observe_args"] = True
        return json.dumps(msg, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"

    def _views(self, o: CallResult, c: CallResult) -> tuple[Any, Any]:
        if self.compare_inputs == "always":
            return [o.out, o.args_after], [c.out, c.args_after]
        if self.compare_inputs == "auto" and o.out is None and c.out is None:
            return o.args_after, c.args_after
        return o.out, c.out

    def classify(self, o: CallResult, c: CallResult) -> Outcome:
        if o.status == "skipped" or c.status == "skipped":
            return Outcome("skipped", None, o, c)
        if not o.ok:
            if o.status == "error" and c.status == "error" and (o.exc == c.exc or self.cross_language):
                return Outcome("pass", None, o, c)
            if self.oracle_errors == "fail" and o.status == "error":
                if c.ok:
                    return Outcome("fail", "unexpected_success", o, c, o.exc, c.out)
                if c.status == "error":
                    return Outcome("fail", "exception_mismatch", o, c, o.exc, c.exc)
                return Outcome("fail", f"candidate_{c.status}", o, c)
            return Outcome("invalid", f"oracle_{o.status}", o, c)
        if c.status == "error":
            return Outcome("fail", f"candidate_exception:{c.exc}", o, c)
        if c.status in ("timeout", "crash"):
            return Outcome("fail", f"candidate_{c.status}", o, c)
        ov, cv = self._views(o, c)
        if self.eq(ov, cv):
            return Outcome("pass", None, o, c, ov, cv)
        return Outcome("fail", "mismatch", o, c, ov, cv)

    # -- execution --------------------------------------------------------- #
    @staticmethod
    def _start_pair(o_side: ProcessSide, c_side: ProcessSide) -> None:
        errors: list[BaseException] = []

        def start(side: ProcessSide) -> None:
            try:
                side.start()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t = threading.Thread(target=start, args=(o_side,))
        t.start()
        start(c_side)
        t.join()
        if errors:
            raise errors[0]

    @staticmethod
    def _run_pair(o_side: ProcessSide, c_side: ProcessSide, msgs: list[bytes], deadline: float | None) -> tuple[list[CallResult], list[CallResult]]:
        box: dict[str, Any] = {}

        def run_oracle() -> None:
            try:
                box["o"] = o_side.run(msgs, deadline=deadline)
            except BaseException as exc:  # noqa: BLE001
                box["err"] = exc

        t = threading.Thread(target=run_oracle)
        t.start()
        c_res = c_side.run(msgs, deadline=deadline)
        t.join()
        if "err" in box:
            raise box["err"]
        return box["o"], c_res

    def spawn_pair(self) -> tuple[ProcessSide, ProcessSide]:
        o_side = self.oracle.spawn(self.timeout_s)
        c_side = self.candidate.spawn(self.timeout_s)
        self._start_pair(o_side, c_side)
        return o_side, c_side

    def run_cases(self, cases: list[dict[str, Any]], *, deadline: float, max_failures: int, max_timeouts: int = 5) -> list[Outcome | None]:
        msgs = [self.message(i, c) for i, c in enumerate(cases)]
        outcomes: list[Outcome | None] = [None] * len(cases)
        n = len(msgs)
        shards = max(1, min(self.workers, math.ceil(n / 300)))
        chunk = max(25, min(500, math.ceil(n / (shards * 4))))
        work: queue.Queue[tuple[int, int]] = queue.Queue()
        for lo in range(0, n, chunk):
            work.put((lo, min(n, lo + chunk)))
        stop = threading.Event()
        lock = threading.Lock()
        failures = [0]
        # Hangs cost a full timeout each, so they stop the run much sooner.
        slow = [0]
        errors: list[BaseException] = []

        def shard() -> None:
            o_side = c_side = None
            try:
                o_side, c_side = self.spawn_pair()
                while not stop.is_set():
                    try:
                        lo, hi = work.get_nowait()
                    except queue.Empty:
                        return
                    o_res, c_res = self._run_pair(o_side, c_side, msgs[lo:hi], deadline)
                    local_fail = local_slow = 0
                    for k, (o, c) in enumerate(zip(o_res, c_res)):
                        out = self.classify(o, c)
                        outcomes[lo + k] = out
                        local_fail += out.verdict == "fail"
                        local_slow += o.status == "timeout" or c.status == "timeout"
                    with lock:
                        failures[0] += local_fail
                        slow[0] += local_slow
                        if failures[0] >= max_failures or slow[0] >= max_timeouts or time.monotonic() > deadline:
                            stop.set()
            except BaseException as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)
                stop.set()
            finally:
                for side in (o_side, c_side):
                    if side is not None:
                        side.close()

        threads = [threading.Thread(target=shard, name=f"arena-shard-{i}") for i in range(shards)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if errors and not any(o is not None for o in outcomes):
            raise errors[0]
        return outcomes

    # -- shrinking --------------------------------------------------------- #
    def shrink(self, case: dict[str, Any], outcome: Outcome, *, max_evals: int, budget_s: float) -> tuple[dict[str, Any], Outcome, dict[str, Any]]:
        target = outcome.kind
        o_side, c_side = self.spawn_pair()
        counter = [0]

        def evaluate(batch: list[dict[str, Any]]) -> tuple[int, Outcome] | None:
            msgs = []
            for case_ in batch:
                counter[0] += 1
                msgs.append(self.message(counter[0], case_))
            o_res, c_res = self._run_pair(o_side, c_side, msgs, None)
            for i, (o, c) in enumerate(zip(o_res, c_res)):
                out = self.classify(o, c)
                if out.verdict == "fail" and out.kind == target:
                    return i, out
            return None

        if target == "candidate_timeout":
            max_evals = min(max_evals, 40)
        try:
            shrinker = DeltaShrinker(self.case_gen, evaluate, max_evals=max_evals, budget_s=budget_s, batch_size=1 if target == "candidate_timeout" else 8)
            result = shrinker.shrink(case, outcome)
        finally:
            o_side.close()
            c_side.close()
        return result.case, result.failure, result.summary()

    def trace(self, case: dict[str, Any]) -> dict[str, Any] | None:
        if not self.candidate.supports_trace:
            return None
        args, kwargs = self.case_gen.materialize(case)
        side = self.candidate.spawn(max(self.timeout_s, 5.0))
        try:
            side.start()
            res = side.call({"op": "trace", "id": 0, "args": encode(args), "kwargs": encode(kwargs), "max_events": 150})
        except WorkerError:
            return None
        finally:
            side.close()
        if "trace" not in res.extra:
            return None
        return {"events": res.extra["trace"], "truncated": res.extra.get("truncated", False)}


# --------------------------------------------------------------------------- #
# Rendering helpers
# --------------------------------------------------------------------------- #


def _call_text(name: str, args: list[Any], kwargs: dict[str, Any], limit: int = 1500) -> str:
    parts = [py_literal(a) for a in args] + [f"{k}={py_literal(v)}" for k, v in kwargs.items()]
    text = f"{name}({', '.join(parts)})"
    return text if len(text) <= limit else text[: limit - 20] + " ...<truncated>)"


def _speedup_text(speedup: float | None) -> str | None:
    if speedup is None:
        return None
    if speedup >= 1:
        return f"{speedup:.1f}x faster"
    return f"{1 / speedup:.1f}x slower"


def _error_dict(res: CallResult) -> dict[str, Any]:
    out: dict[str, Any] = {"status": res.status}
    for key in ("exc", "msg", "line", "tb", "stdout", "stderr"):
        val = getattr(res, key)
        if val:
            out[key] = val
    return out


def _build_cases(case_gen: CaseGenerator, num_runs: int, seed: int, edge_cases: bool) -> tuple[list[dict[str, Any]], int]:
    rng = random.Random(seed)
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(case: dict[str, Any]) -> bool:
        args, kwargs = case_gen.materialize(case)
        key = json.dumps([encode(args), encode(kwargs)], sort_keys=True, separators=(",", ":"))
        if key in seen:
            return False
        seen.add(key)
        cases.append(case)
        return True

    n_edges = 0
    if edge_cases:
        for case in case_gen.edge_cases():
            if len(cases) >= num_runs:
                break
            n_edges += add(case)
    attempts = 0
    ramp = max(1.0, 0.7 * num_runs)
    while len(cases) < num_runs and attempts < num_runs * 6 + 100:
        attempts += 1
        # Ramp by attempts, not distinct cases: tiny sizes repeat a lot and
        # would otherwise stall the ramp.
        size = min(1.0, 0.02 + attempts / ramp)
        add(case_gen.generate(rng, size))
    return cases, n_edges


def _error(message: str, **extra: Any) -> dict[str, Any]:
    return {"status": "ERROR", "error": message, **extra}


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def run_differential_arena(
    oracle_code: str,
    candidate_code: str,
    generator_spec: Any = None,
    num_runs: int | None = None,
    timeout_ms: int | None = None,
    *,
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
    shrink: bool = True,
    trace: bool = True,
    edge_cases: bool = True,
    max_failures: int = 1000,
    budget_s: float | None = None,
    workers: int | None = None,
    cxx_std: str | None = None,
) -> dict[str, Any]:
    """Falsify ``candidate_code`` against ``oracle_code`` on adversarial inputs.

    Returns ``status`` = ``VERIFIED`` (no divergence), ``FALSIFIED`` (with the
    minimal shrunk counterexample, exact output diff and a regression payload),
    ``INCONCLUSIVE`` (every input was rejected by the oracle) or ``ERROR``.
    """
    settings = get_settings()
    t_start = time.monotonic()
    num_runs = int(num_runs or settings.default_arena_runs)
    if num_runs <= 0:
        return _error("num_runs must be positive")
    timeout_s = max(0.01, (timeout_ms or settings.sandbox_timeout_ms) / 1000.0)
    budget = float(budget_s or settings.arena_budget_s)
    deadline = t_start + budget
    if comparator not in COMPARATOR_MODES:
        return _error(f"unknown comparator {comparator!r}; choose from {', '.join(COMPARATOR_MODES)}")
    if compare_inputs not in ("auto", "always", "never"):
        return _error("compare_inputs must be 'auto', 'always' or 'never'")
    if oracle_errors not in ("discard", "fail"):
        return _error("oracle_errors must be 'discard' or 'fail'")

    try:
        oracle = make_implementation(oracle_code, role="oracle", language=oracle_language or language, entrypoint=oracle_entrypoint, cxx_std=cxx_std)
        candidate = make_implementation(candidate_code, role="candidate", language=candidate_language or language, entrypoint=candidate_entrypoint, cxx_std=cxx_std)
    except WorkerError as exc:
        return _error(str(exc), phase="load", details=exc.info)

    spec_inferred = False
    if generator_spec in (None, "", {}):
        try:
            generator_spec = oracle.infer_spec()
        except (SpecError, NotImplementedError, WorkerError) as first:
            try:
                generator_spec = candidate.infer_spec()
            except (SpecError, NotImplementedError, WorkerError):
                return _error(f"generator_spec is required: {first}", phase="spec")
        spec_inferred = True
    if isinstance(generator_spec, str) and generator_spec.strip().startswith(("{", "[")):
        try:
            generator_spec = json.loads(generator_spec)
        except ValueError as exc:
            return _error(f"generator_spec is not valid JSON: {exc}", phase="spec")
    try:
        case_gen = CaseGenerator(generator_spec)
    except SpecError as exc:
        return _error(f"invalid generator_spec: {exc}", phase="spec")

    try:
        compile_errors: list[BaseException] = []

        def prep(impl: Implementation) -> None:
            try:
                impl.prepare()
            except BaseException as exc:  # noqa: BLE001
                compile_errors.append(exc)

        t = threading.Thread(target=prep, args=(oracle,))
        t.start()
        prep(candidate)
        t.join()
        if compile_errors:
            raise compile_errors[0]
    except WorkerError as exc:
        return _error(str(exc), phase="compile", details=exc.info)

    arena = _Arena(
        oracle,
        candidate,
        case_gen,
        comparator=comparator,
        float_tolerance=float_tolerance,
        compare_inputs=compare_inputs,
        oracle_errors=oracle_errors,
        timeout_s=timeout_s,
        workers=workers or settings.max_workers,
    )
    cases, n_edges = _build_cases(case_gen, num_runs, seed, edge_cases)
    try:
        outcomes = arena.run_cases(cases, deadline=deadline, max_failures=max(1, max_failures))
    except WorkerError as exc:
        return _error(str(exc), phase="startup", details=exc.info)

    executed = [(i, o) for i, o in enumerate(outcomes) if o is not None and o.verdict != "skipped"]
    passed = [i for i, o in executed if o.verdict == "pass"]
    failed = [(i, o) for i, o in executed if o.verdict == "fail"]
    invalid = [(i, o) for i, o in executed if o.verdict == "invalid"]
    valid = len(passed) + len(failed)

    o_ns = sum((o.oracle.ns or 0) for _, o in executed if o.verdict == "pass" and o.oracle.ns)
    c_ns = sum((o.candidate.ns or 0) for _, o in executed if o.verdict == "pass" and o.candidate.ns)
    # Keep the raw positive ratio.  Rounding a very slow candidate to 0.0
    # makes the human-readable "x slower" conversion divide by zero.
    speedup = o_ns / c_ns if o_ns and c_ns else None

    kinds: dict[str, int] = {}
    for _, o in failed:
        kinds[o.kind or "?"] = kinds.get(o.kind or "?", 0) + 1
    discard_kinds: dict[str, int] = {}
    for _, o in invalid:
        discard_kinds[o.kind or "?"] = discard_kinds.get(o.kind or "?", 0) + 1

    o_name = oracle.entrypoint
    c_name = candidate.entrypoint
    result: dict[str, Any] = {
        "status": "VERIFIED",
        "summary": "",
        "oracle": oracle.describe(),
        "candidate": candidate.describe(),
        "generator_spec": generator_spec,
        "spec_inferred": spec_inferred,
        "seed": seed,
        "runs": {
            "requested": num_runs,
            "generated": len(cases),
            "edge_cases": n_edges,
            "executed": len(executed),
            "valid": valid,
            "passed": len(passed),
            "failed": len(failed),
            "discarded": len(invalid),
            "skipped": len(cases) - len(executed),
        },
        "pass_rate": round(len(passed) / valid, 4) if valid else None,
        "failure_kinds": kinds,
        "timing": {
            "oracle_ms_total": round(o_ns / 1e6, 3),
            "candidate_ms_total": round(c_ns / 1e6, 3),
            "speedup": speedup,
            "speedup_text": _speedup_text(speedup),
        },
    }
    if invalid:
        samples = []
        for i, o in invalid[:3]:
            args, kwargs = case_gen.materialize(cases[i])
            samples.append({"input": _call_text(o_name, args, kwargs, 300), "oracle": o.oracle.error_summary()})
        result["discarded_samples"] = samples
        result["discard_kinds"] = discard_kinds
        result["warnings"] = [
            f"{len(invalid)} inputs made the oracle fail ({', '.join(discard_kinds)}); they were discarded. "
            "Tighten generator_spec to the oracle's preconditions, or pass oracle_errors='fail' if exception "
            "behaviour is part of the contract (e.g. a refactor)."
        ]
    if len(cases) < num_runs:
        result.setdefault("warnings", []).append(
            f"only {len(cases)} distinct inputs exist for this generator_spec (requested {num_runs}); the input space was exhausted."
        )
    if result["runs"]["skipped"]:
        if time.monotonic() > deadline:
            reason = f"time budget of {budget:.0f}s exhausted"
        elif len(failed) >= max_failures:
            reason = f"stopped after {len(failed)} failures (max_failures)"
        else:
            reason = "stopped early after repeated timeouts"
        result.setdefault("warnings", []).append(f"{result['runs']['skipped']} inputs not executed: {reason}.")

    if not failed:
        result["timing"]["wall_ms"] = round((time.monotonic() - t_start) * 1000, 1)
        if valid == 0:
            result["status"] = "INCONCLUSIVE"
            result["summary"] = "No valid runs: the oracle rejected every generated input. Adjust generator_spec."
            result["next_steps"] = ["Inspect discarded_samples and constrain generator_spec to inputs the oracle accepts."]
            return result
        sp = f" Candidate is {_speedup_text(speedup)} than the oracle." if speedup else ""
        result["summary"] = f"VERIFIED: candidate matched the oracle on {valid}/{valid} valid adversarial inputs.{sp}"
        result["next_steps"] = [
            "Run profile_empirical_complexity on the candidate to confirm its asymptotic growth.",
            "For final verification raise num_runs (e.g. 10000) or widen generator_spec (sizes, value ranges, special values).",
        ]
        return result

    # ---- FALSIFIED: pick the simplest failure of the highest-priority kind ----
    first_idx, first = min(failed, key=lambda io: (_kind_priority(io[1].kind or ""), case_gen.size_of(cases[io[0]]), io[0]))
    original_case = cases[first_idx]
    o_args, o_kwargs = case_gen.materialize(original_case)
    result["status"] = "FALSIFIED"
    result["failure_kind"] = first.kind
    result["original_counterexample"] = {
        "call": _call_text(c_name, o_args, o_kwargs, 600),
        "size": case_gen.size_of(original_case),
        "run_index": first_idx,
    }

    shrunk_case, final, shrink_stats = original_case, first, None
    if shrink:
        remaining = max(5.0, deadline - time.monotonic())
        try:
            shrunk_case, final, shrink_stats = arena.shrink(
                original_case, first, max_evals=settings.shrink_max_evals, budget_s=min(60.0, remaining)
            )
        except WorkerError as exc:
            result.setdefault("warnings", []).append(f"shrinking aborted: {exc}")

    args, kwargs = case_gen.materialize(shrunk_case)
    result["shrunk_counterexample"] = {
        "call": _call_text(c_name, args, kwargs),
        "args": [py_literal(a) for a in args],
        "kwargs": {k: py_literal(v) for k, v in kwargs.items()},
        "size": case_gen.size_of(shrunk_case),
    }
    if shrink_stats:
        result["shrink_stats"] = shrink_stats

    kind = final.kind or "mismatch"
    expected_kind = "return"
    expected_value: Any = final.oracle.out
    if final.oracle.ok and arena.compare_inputs == "auto" and final.oracle.out is None and final.candidate.ok and final.candidate.out is None:
        expected_kind = "args_after"
        expected_value = final.oracle.args_after
    if kind == "mismatch":
        entries = structural_diff(final.oracle_view, final.candidate_view, arena.eq)
        result["oracle_output"] = short_repr(final.oracle_view, 2000)
        result["candidate_output"] = short_repr(final.candidate_view, 2000)
        result["diff"] = entries
        result["diff_summary"] = summarize(entries)
        td = text_diff(final.oracle_view, final.candidate_view)
        if td:
            result["text_diff"] = td
        detail = result["diff_summary"]
        if expected_kind == "args_after":
            result["note"] = "Both implementations return None, so their in-place mutations of the arguments were compared."
            repro = f"args = {py_literal(args)}; {c_name}(*args); assert args == {py_literal(final.oracle.args_after)}"
        else:
            repro = f"assert {_call_text(c_name, args, kwargs)} == {py_literal(final.oracle.out)}"
    elif kind in ("unexpected_success", "exception_mismatch"):
        result["oracle_error"] = _error_dict(final.oracle)
        if final.candidate.ok:
            result["candidate_output"] = short_repr(final.candidate.out, 2000)
        else:
            result["candidate_error"] = _error_dict(final.candidate)
        expected_kind = "exception"
        detail = f"oracle raises {final.oracle.exc} but candidate " + (
            f"returns {short_repr(final.candidate.out, 200)}" if final.candidate.ok else f"raises {final.candidate.exc}"
        )
        repro = f"{_call_text(c_name, args, kwargs)}  # oracle raises {final.oracle.exc}"
    else:
        result["oracle_output"] = short_repr(final.oracle.out, 2000)
        result["candidate_error"] = _error_dict(final.candidate)
        what = {"candidate_timeout": f"timed out after {timeout_s * 1000:.0f} ms", "candidate_crash": "crashed the process"}.get(
            kind, f"raised {final.candidate.exc}: {final.candidate.msg}"
        )
        detail = f"candidate {what}" + (f" (line {final.candidate.line})" if final.candidate.line else "")
        repro = f"{_call_text(c_name, args, kwargs)}  # expected {py_literal(final.oracle.out)}; candidate {what}"
    result["repro"] = repro

    if trace and kind != "candidate_timeout":
        tr = arena.trace(shrunk_case)
        if tr:
            result["candidate_trace"] = tr

    payload: dict[str, Any] = {
        "schema": PAYLOAD_SCHEMA,
        "function": c_name,
        "oracle_function": o_name,
        "language": candidate.language,
        "args": encode(args),
        "kwargs": encode(kwargs),
        "expected_kind": expected_kind,
        "expected": encode(expected_value) if expected_kind != "exception" else None,
        "expected_exception": final.oracle.exc if expected_kind == "exception" else None,
        "failure_kind": kind,
        "observed": encode(final.candidate.out) if final.candidate.ok else None,
        "observed_error": final.candidate.exc if not final.candidate.ok else None,
        "comparator": comparator,
        "float_tolerance": float_tolerance,
        "seed": seed,
    }
    cpp_sig = getattr(candidate, "signature", None)
    if cpp_sig:
        payload["cpp_signature"] = cpp_sig
    result["counterexample_payload"] = payload

    size_note = ""
    if shrink_stats and shrink_stats["original_size"] != shrink_stats["shrunk_size"]:
        size_note = f" (shrunk from size {shrink_stats['original_size']} to {shrink_stats['shrunk_size']})"
    result["summary"] = (
        f"FALSIFIED: {len(failed)}/{valid} valid inputs diverged. Minimal counterexample{size_note}: "
        f"{result['shrunk_counterexample']['call']} -> {detail}."
    )
    result["next_steps"] = [
        "Do not guess at fixes blindly: trace the candidate step by step on shrunk_counterexample "
        "(candidate_trace shows each executed line and the locals it changed).",
        "Identify the exact line where candidate state diverges from oracle state, patch the candidate, "
        f"and re-run run_differential_arena with seed={seed} until status is VERIFIED.",
        "Then call lock_regression_test with counterexample_payload to keep this input as a permanent unit test.",
    ]
    result["timing"]["wall_ms"] = round((time.monotonic() - t_start) * 1000, 1)
    return result
