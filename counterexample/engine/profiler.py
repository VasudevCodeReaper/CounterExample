"""Empirical Big-O profiler.

Runs the candidate on inputs of size N = 10, 100, 1000, 10000 (or any
``scale_steps``), measures median wall time (and peak memory for Python via
``tracemalloc``), fits the classic complexity models with least squares and
flags silent super-linear regressions before they reach production.
"""

from __future__ import annotations

import json
import math
import random
import time
from typing import Any

from counterexample.adapters import WorkerError, make_implementation
from counterexample.codec import encode
from counterexample.config import get_settings
from counterexample.generators import CaseGenerator, SpecError

DEFAULT_STEPS = [10, 100, 1000, 10000]

# name -> (rank, f(n)); rank orders models from cheapest to most expensive.
MODELS: dict[str, tuple[int, Any]] = {
    "O(1)": (0, lambda n: 1.0),
    "O(log N)": (1, lambda n: math.log2(max(n, 2))),
    "O(N)": (2, lambda n: float(n)),
    "O(N log N)": (3, lambda n: n * math.log2(max(n, 2))),
    "O(N^2)": (4, lambda n: float(n) ** 2),
    "O(N^2 log N)": (5, lambda n: float(n) ** 2 * math.log2(max(n, 2))),
    "O(N^3)": (6, lambda n: float(n) ** 3),
    "O(2^N)": (7, lambda n: 2.0 ** min(n, 1000)),
}

# Log-log slope each model shows asymptotically (used to judge regressions).
NOMINAL_SLOPE = {
    "O(1)": 0.0, "O(log N)": 0.15, "O(N)": 1.0, "O(N log N)": 1.15,
    "O(N^2)": 2.0, "O(N^2 log N)": 2.15, "O(N^3)": 3.0, "O(2^N)": 10.0,
}

_ALIASES = {
    "1": "O(1)", "constant": "O(1)", "logn": "O(log N)", "log n": "O(log N)", "n": "O(N)", "linear": "O(N)",
    "nlogn": "O(N log N)", "n log n": "O(N log N)", "n^2": "O(N^2)", "n2": "O(N^2)", "quadratic": "O(N^2)",
    "n^2 log n": "O(N^2 log N)", "n^3": "O(N^3)", "cubic": "O(N^3)", "2^n": "O(2^N)", "exponential": "O(2^N)",
}


def normalize_complexity(text: str | None) -> str | None:
    if not text:
        return None
    t = text.strip()
    if t in MODELS:
        return t
    key = t.lower().replace("o(", "").replace(")", "").replace("*", " ").replace("·", " ").strip()
    key = " ".join(key.split())
    if key in _ALIASES:
        return _ALIASES[key]
    compact = key.replace(" ", "")
    for alias, name in _ALIASES.items():
        if alias.replace(" ", "") == compact:
            return name
    return None


def fit_models(ns: list[float], ys: list[float]) -> list[dict[str, Any]]:
    """Fit y ~ a + b*f(n) (b >= 0) for each model; return fits sorted by error."""
    fits = []
    for name, (rank, f) in MODELS.items():
        try:
            xs = [f(n) for n in ns]
        except OverflowError:
            continue
        if any(math.isinf(x) or math.isnan(x) or x > 1e150 for x in xs):
            continue  # e.g. 2^N at N=1000 cannot be fitted meaningfully
        k = len(xs)
        mx, my = sum(xs) / k, sum(ys) / k
        sxx = sum((x - mx) ** 2 for x in xs)
        if sxx == 0:
            a, b = my, 0.0
        else:
            b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
            if b < 0:
                b = 0.0
            a = my - b * mx
            if a < 0:  # no negative fixed overhead: refit through the origin
                a = 0.0
                b = sum(x * y for x, y in zip(xs, ys)) / max(sum(x * x for x in xs), 1e-300)
        preds = [a + b * x for x in xs]
        rel = math.sqrt(sum(((p - y) / y) ** 2 for p, y in zip(preds, ys) if y > 0) / k)
        fits.append({"model": name, "rank": rank, "rel_error": rel, "a": a, "b": b})
    fits.sort(key=lambda fit: (round(fit["rel_error"], 2), fit["rank"]))
    return fits


def loglog_slope(ns: list[float], ys: list[float]) -> float | None:
    pts = [(math.log(n), math.log(y)) for n, y in zip(ns, ys) if n > 0 and y > 0]
    if len(pts) < 2:
        return None
    mx = sum(p[0] for p in pts) / len(pts)
    my = sum(p[1] for p in pts) / len(pts)
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    if sxx == 0:
        return None
    return sum((p[0] - mx) * (p[1] - my) for p in pts) / sxx


def _classify(ns: list[float], ys: list[float]) -> dict[str, Any] | None:
    if len(ns) < 2:
        return None
    fits = fit_models(ns, ys)
    slope = loglog_slope(ns, ys)
    tail = loglog_slope(ns[-2:], ys[-2:])
    best = fits[0]
    # The asymptotic slope dominates once the fixed per-call overhead fades.
    if tail is not None:
        ranked_by_slope = {0: "O(1)", 1: "O(N)", 2: "O(N^2)", 3: "O(N^3)"}
        nearest = min(ranked_by_slope, key=lambda k: abs(k - tail))
        candidates = {
            "O(1)": ["O(1)", "O(log N)"],
            "O(N)": ["O(N)", "O(N log N)"],
            "O(N^2)": ["O(N^2)", "O(N^2 log N)"],
            "O(N^3)": ["O(N^3)", "O(2^N)"],
        }[ranked_by_slope[nearest]]
        if best["model"] not in candidates and abs(nearest - tail) < 0.35:
            best = next((f for f in fits if f["model"] in candidates), best)
    return {
        "best_fit": best["model"],
        "loglog_slope": round(slope, 3) if slope is not None else None,
        "asymptotic_slope": round(tail, 3) if tail is not None else None,
        "fits": [{"model": f["model"], "rel_error": round(f["rel_error"], 4)} for f in fits[:5]],
        "_best": best,
    }


def profile_empirical_complexity(
    candidate_code: str,
    generator_spec: Any = None,
    scale_steps: list[int] | None = None,
    *,
    entrypoint: str | None = None,
    language: str = "auto",
    repeats: int = 5,
    timeout_ms: int | None = None,
    expected_complexity: str | None = None,
    memory: bool = True,
    seed: int = 0,
    cxx_std: str | None = None,
) -> dict[str, Any]:
    """Measure how the candidate's time (and memory) grow with input size N."""
    settings = get_settings()
    steps = sorted({int(s) for s in (scale_steps or DEFAULT_STEPS) if int(s) > 0})
    if len(steps) < 2:
        return {"status": "ERROR", "error": "scale_steps needs at least two distinct positive sizes"}
    per_step_timeout = max(5.0, (timeout_ms or settings.sandbox_timeout_ms) / 1000.0 * max(1, repeats) * 2)

    try:
        impl = make_implementation(candidate_code, role="candidate", language=language, entrypoint=entrypoint, cxx_std=cxx_std)
    except WorkerError as exc:
        return {"status": "ERROR", "error": str(exc), "phase": "load", "details": exc.info}
    spec_inferred = False
    if generator_spec in (None, "", {}):
        try:
            generator_spec = impl.infer_spec()
            spec_inferred = True
        except (SpecError, NotImplementedError) as exc:
            return {"status": "ERROR", "error": f"generator_spec is required: {exc}", "phase": "spec"}
    if isinstance(generator_spec, str) and generator_spec.strip().startswith(("{", "[")):
        generator_spec = json.loads(generator_spec)
    try:
        case_gen = CaseGenerator(generator_spec)
    except SpecError as exc:
        return {"status": "ERROR", "error": f"invalid generator_spec: {exc}", "phase": "spec"}
    try:
        impl.prepare()
    except WorkerError as exc:
        return {"status": "ERROR", "error": str(exc), "phase": "compile", "details": exc.info}

    rng = random.Random(seed)
    rows: list[dict[str, Any]] = []
    flags: list[str] = []
    side = impl.spawn(per_step_timeout)
    t_start = time.monotonic()
    try:
        side.start()
        for n in steps:
            case = case_gen.scaled(rng, n)
            args, kwargs = case_gen.materialize(case)
            msg = {
                "op": "profile",
                "id": n,
                "args": encode(args),
                "kwargs": encode(kwargs),
                "repeat": repeats,
                "min_time_ns": 2_000_000,
                "budget_ns": int(per_step_timeout * 0.8e9),
                "memory": memory,
            }
            res = side.call(msg, timeout_s=per_step_timeout)
            row: dict[str, Any] = {"n": n, "input_size": case_gen.size_of(case)}
            if res.status == "timeout":
                row["status"] = "timeout"
                rows.append(row)
                flags.append(f"TIMEOUT: N={n} did not finish within {per_step_timeout:.0f}s - growth is steeper than the previous steps suggest.")
                break
            if not res.ok:
                row["status"] = res.status
                row["error"] = res.error_summary()
                rows.append(row)
                flags.append(f"ERROR at N={n}: {res.error_summary()}")
                break
            row["status"] = "ok"
            row["time_ms_median"] = round(res.extra["ns_median"] / 1e6, 6)
            row["time_ms_min"] = round(res.extra["ns_min"] / 1e6, 6)
            if res.extra.get("peak_bytes") is not None:
                row["peak_memory_kb"] = round(res.extra["peak_bytes"] / 1024, 2)
            rows.append(row)
    except WorkerError as exc:
        return {"status": "ERROR", "error": str(exc), "phase": "startup", "details": exc.info}
    finally:
        side.close()

    ok_rows = [r for r in rows if r.get("status") == "ok"]
    ns = [float(r["n"]) for r in ok_rows]
    times = [r["time_ms_median"] for r in ok_rows]
    result: dict[str, Any] = {
        "status": "OK",
        "candidate": impl.describe(),
        "generator_spec": generator_spec,
        "spec_inferred": spec_inferred,
        "steps": rows,
        "wall_ms": round((time.monotonic() - t_start) * 1000, 1),
    }
    time_fit = _classify(ns, times)
    if time_fit:
        best = time_fit.pop("_best")
        result["time_complexity"] = time_fit
        rank = MODELS[time_fit["best_fit"]][0]
        slope = time_fit["asymptotic_slope"] or 0
        if rank >= MODELS["O(N^2)"][0] or slope > 1.6:
            flags.append(
                f"SUPERLINEAR: time grows like {time_fit['best_fit']} (log-log slope {slope:.2f}); "
                "a silent O(N^2)-or-worse path will hurt at production sizes."
            )
        projections = {}
        for big in (10**5, 10**6):
            if big > steps[-1]:
                f = MODELS[best["model"]][1]
                try:
                    projections[f"N={big:,}"] = f"{(best['a'] + best['b'] * f(big)) / 1000:.3g} s"
                except OverflowError:
                    projections[f"N={big:,}"] = "overflow"
        if projections:
            result["projected_time"] = projections
        want = normalize_complexity(expected_complexity)
        if expected_complexity and want is None:
            flags.append(f"could not parse expected_complexity {expected_complexity!r}")
        if want:
            result["expected_complexity"] = want
            if rank > MODELS[want][0] and slope > NOMINAL_SLOPE[want] + 0.4:
                flags.append(f"REGRESSION: expected {want} but measured {time_fit['best_fit']} (slope {slope:.2f}).")
    mem_rows = [r for r in ok_rows if r.get("peak_memory_kb")]
    if len(mem_rows) >= 2:
        mem = [r["peak_memory_kb"] for r in mem_rows]
        mem_fit = _classify([float(r["n"]) for r in mem_rows], mem)
        if mem_fit and max(mem) - min(mem) < 2.0:
            # A couple of KB of jitter is allocator noise, not growth.
            mem_fit["best_fit"] = "O(1)"
        if mem_fit:
            mem_fit.pop("_best", None)
            result["memory_complexity"] = mem_fit
            if MODELS[mem_fit["best_fit"]][0] >= MODELS["O(N^2)"][0]:
                flags.append(f"MEMORY: peak memory grows like {mem_fit['best_fit']}.")
    elif impl.language == "cpp":
        result["memory_complexity"] = None
        result.setdefault("notes", []).append("memory profiling is available for Python candidates only")

    result["flags"] = flags
    if any(f.startswith(("SUPERLINEAR", "REGRESSION", "TIMEOUT", "MEMORY")) for f in flags):
        result["status"] = "FLAGGED"
    if any(f.startswith("ERROR") for f in flags) and len(ok_rows) < 2:
        result["status"] = "ERROR"
        result["error"] = flags[-1]
    tc = result.get("time_complexity")
    result["summary"] = (
        f"{result['status']}: empirical time complexity {tc['best_fit']} (log-log slope {tc['asymptotic_slope']})"
        + (f"; flags: {' | '.join(flags)}" if flags else "")
        if tc
        else f"{result['status']}: not enough successful steps to fit a curve" + (f"; {flags[-1]}" if flags else "")
    )
    return result
