"""Generator protocol, registry and the top-level argument (case) generator.

A *generator spec* is plain JSON so agents can write it inline::

    {"type": "list", "elements": {"type": "int", "min": -5, "max": 5}, "max_len": 30}

or as a shorthand string such as ``"list[int(-5,5)]"``.  A spec for a whole
function call lists one spec per positional argument::

    {"args": [{"type": "graph", "spread": true}, {"type": "int", "min": 0, "max": 9}],
     "kwargs": {"strict": {"type": "bool"}}}

Every generator knows how to (1) sample a value, (2) enumerate boundary values,
(3) sample at an exact scale ``N`` for the complexity profiler and (4) propose
strictly smaller variants of a value for the delta shrinker.
"""

from __future__ import annotations

import random
import re
from typing import Any, Callable, Iterator

from counterexample.codec import value_size


class SpecError(ValueError):
    """Raised when a generator spec is malformed."""


_REGISTRY: dict[str, type["Generator"]] = {}


def register(*names: str) -> Callable[[type["Generator"]], type["Generator"]]:
    def deco(cls: type["Generator"]) -> type["Generator"]:
        for name in names:
            _REGISTRY[name] = cls
        cls.type_name = names[0]
        return cls

    return deco


def available_types() -> list[str]:
    _ensure_loaded()
    return sorted(_REGISTRY)


def _ensure_loaded() -> None:
    # Importing the modules populates the registry through @register.
    from counterexample.generators import graphs, http, intervals, matrices, primitives  # noqa: F401


class Generator:
    """Base class for all composable generators."""

    type_name = "?"
    # Collections grow with N in the complexity profiler; scalars do not.
    scalable = False

    def __init__(self, spec: dict[str, Any]):
        self.spec = spec

    # -- sampling ---------------------------------------------------------- #
    def generate(self, rng: random.Random, size: float) -> Any:
        """Sample a value. ``size`` in [0, 1] ramps collection sizes up over a run."""
        raise NotImplementedError

    def edge_cases(self) -> list[Any]:
        """Deterministic boundary values (0, -1, empty, singleton, extremes...)."""
        return []

    def scaled(self, rng: random.Random, n: int) -> Any:
        """Sample a value whose dominant dimension is exactly ``n``."""
        return self.generate(rng, 1.0)

    # -- shrinking --------------------------------------------------------- #
    def shrink(self, value: Any) -> Iterator[Any]:
        """Yield strictly simpler variants of ``value``, most aggressive first."""
        return iter(())

    def validate(self, value: Any) -> bool:
        return True

    def describe(self) -> dict[str, Any]:
        return {"type": self.type_name, **{k: v for k, v in self.spec.items() if k != "type"}}

    # -- helpers ----------------------------------------------------------- #
    def opt(self, key: str, default: Any = None) -> Any:
        return self.spec.get(key, default)


def build(spec: Any) -> Generator:
    """Instantiate a generator from a spec dict, shorthand string or list."""
    _ensure_loaded()
    if isinstance(spec, Generator):
        return spec
    if isinstance(spec, str):
        spec = parse_shorthand(spec)
    if isinstance(spec, list):
        spec = {"type": "tuple", "items": spec}
    if not isinstance(spec, dict):
        raise SpecError(f"generator spec must be an object or string, got {type(spec).__name__}")
    kind = spec.get("type")
    if kind is None:
        raise SpecError(f"generator spec is missing 'type': {spec!r}")
    cls = _REGISTRY.get(kind)
    if cls is None:
        raise SpecError(f"unknown generator type {kind!r}; available: {', '.join(sorted(_REGISTRY))}")
    return cls(spec)


# --------------------------------------------------------------------------- #
# Shorthand grammar:  int | int(lo,hi) | float(lo,hi) | list[T] | set[T] |
#                     tuple[T, U] | dict[K, V] | optional[T] | <any type name>
# --------------------------------------------------------------------------- #

_TOKEN = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*|-?\d+(?:\.\d+)?(?:e-?\d+)?|[\[\](),])")


def parse_shorthand(text: str) -> dict[str, Any]:
    tokens = _TOKEN.findall(text)
    if "".join(tokens).replace(" ", "") != text.replace(" ", ""):
        raise SpecError(f"cannot parse generator shorthand {text!r}")
    pos = 0

    def peek() -> str | None:
        return tokens[pos] if pos < len(tokens) else None

    def take(expected: str | None = None) -> str:
        nonlocal pos
        if pos >= len(tokens):
            raise SpecError(f"unexpected end of shorthand {text!r}")
        tok = tokens[pos]
        if expected is not None and tok != expected:
            raise SpecError(f"expected {expected!r} in shorthand {text!r}, got {tok!r}")
        pos += 1
        return tok

    def number() -> float | int:
        tok = take()
        try:
            return int(tok)
        except ValueError:
            return float(tok)

    def parse() -> dict[str, Any]:
        name = take().lower()
        aliases = {"integer": "int", "string": "str", "boolean": "bool", "array": "list"}
        name = aliases.get(name, name)
        if peek() == "(":
            take("(")
            lo = number()
            take(",")
            hi = number()
            take(")")
            if name not in ("int", "float"):
                raise SpecError(f"range arguments only apply to int/float, not {name!r}")
            return {"type": name, "min": lo, "max": hi}
        if peek() == "[":
            take("[")
            inner = [parse()]
            while peek() == ",":
                take(",")
                inner.append(parse())
            take("]")
            if name in ("list", "set"):
                return {"type": name, "elements": inner[0]}
            if name == "tuple":
                return {"type": "tuple", "items": inner}
            if name == "dict":
                if len(inner) != 2:
                    raise SpecError("dict[K, V] needs exactly two type arguments")
                return {"type": "dict", "keys": inner[0], "values": inner[1]}
            if name == "optional":
                return {"type": "optional", "of": inner[0]}
            raise SpecError(f"{name!r} does not take type arguments")
        return {"type": name}

    result = parse()
    if pos != len(tokens):
        raise SpecError(f"trailing tokens in shorthand {text!r}")
    return result


# --------------------------------------------------------------------------- #
# Shared shrinking / sizing helpers
# --------------------------------------------------------------------------- #


def parse_range(value: Any, default_lo: int, default_hi: int) -> tuple[int, int]:
    """Accept ``5``, ``[lo, hi]`` or ``{"min": lo, "max": hi}``."""
    if value is None:
        lo, hi = default_lo, default_hi
    elif isinstance(value, bool):
        raise SpecError("range must be a number, [lo, hi] or {min, max}")
    elif isinstance(value, (int, float)):
        lo = hi = int(value)
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        lo, hi = int(value[0]), int(value[1])
    elif isinstance(value, dict):
        lo, hi = int(value.get("min", default_lo)), int(value.get("max", default_hi))
    else:
        raise SpecError(f"invalid range {value!r}")
    if lo > hi:
        raise SpecError(f"range min {lo} exceeds max {hi}")
    return lo, hi


def sized_count(rng: random.Random, lo: int, hi: int, size: float) -> int:
    """Pick a collection length in [lo, hi], scaled by ``size`` and skewed small."""
    span = hi - lo
    if span <= 0:
        return lo
    cap = lo + max(1, round(span * max(0.0, min(1.0, size))))
    r = rng.random()
    if r < 0.1:
        return lo
    if r < 0.2:
        return min(lo + 1, hi)
    if r < 0.55:
        # Skewed toward short collections.
        return lo + int((cap - lo) * rng.random() ** 2)
    return rng.randint(lo, cap)


def remove_chunks(seq: list[Any], min_len: int = 0) -> Iterator[list[Any]]:
    """Delta-debugging (ddmin) style removals: halves, quarters, ..., singles."""
    n = len(seq)
    if n <= min_len:
        return
    yield seq[:min_len]
    chunk = n // 2
    while chunk >= 1:
        for start in range(0, n, chunk):
            cand = seq[:start] + seq[start + chunk :]
            if len(cand) >= min_len and len(cand) < n:
                yield cand
        chunk //= 2


def shrink_int_toward(value: int, target: int) -> Iterator[int]:
    """Binary-search style candidates moving ``value`` toward ``target``."""
    if value == target:
        return
    yield target
    diff = value - target
    step = diff // 2
    seen = {target, value}
    while step != 0:
        cand = value - step
        if cand not in seen:
            seen.add(cand)
            yield cand
        step = step // 2 if step > 0 else -((-step) // 2)
    last = value - (1 if diff > 0 else -1)
    if last not in seen:
        yield last


# --------------------------------------------------------------------------- #
# Whole-call ("case") generator
# --------------------------------------------------------------------------- #


class _Slot:
    __slots__ = ("gen", "derived", "spread", "scale")

    def __init__(self, gen: Generator | None, derived: dict[str, Any] | None, spread: bool, scale: bool | None):
        self.gen = gen
        self.derived = derived
        self.spread = spread
        self.scale = scale


class CaseGenerator:
    """Generates complete call cases ``{"args": [...], "kwargs": {...}}``.

    Accepts either a full call spec (``{"args": [...], "kwargs": {...}}``) or a
    single-argument spec.  Supports two call-level conveniences:

    * ``{"type": "len_of", "arg": 0, "offset": 0}`` - derived argument equal to the
      length of another argument (e.g. ``solve(n, arr)`` with ``n == len(arr)``).
    * ``"spread": true`` on an argument spec - a tuple value such as a graph's
      ``(n, edges)`` is splatted into separate positional arguments.
    """

    def __init__(self, spec: Any):
        if isinstance(spec, dict) and ("args" in spec or "kwargs" in spec) and "type" not in spec:
            arg_specs = spec.get("args", [])
            kw_specs = spec.get("kwargs", {})
        else:
            arg_specs = [spec]
            kw_specs = {}
        if not isinstance(arg_specs, list):
            raise SpecError("'args' must be a list of generator specs")
        if not isinstance(kw_specs, dict):
            raise SpecError("'kwargs' must be an object mapping names to generator specs")
        self.raw_spec = spec
        self.slots: list[_Slot] = []
        for s in arg_specs:
            if isinstance(s, dict) and s.get("type") == "len_of":
                self.slots.append(_Slot(None, s, False, None))
            else:
                spread = bool(s.get("spread", False)) if isinstance(s, dict) else False
                scale = s.get("scale") if isinstance(s, dict) else None
                self.slots.append(_Slot(build(s), None, spread, scale))
        self.kwargs: dict[str, Generator] = {k: build(v) for k, v in kw_specs.items()}
        for slot in self.slots:
            if slot.derived is not None:
                ref = slot.derived.get("arg", 0)
                if isinstance(ref, int) and not (0 <= ref < len(self.slots)):
                    raise SpecError(f"len_of references missing argument {ref}")
                if isinstance(ref, str) and ref not in self.kwargs:
                    raise SpecError(f"len_of references missing kwarg {ref!r}")

    # -- sampling ---------------------------------------------------------- #
    def generate(self, rng: random.Random, size: float) -> dict[str, Any]:
        return {
            "args": [s.gen.generate(rng, size) if s.gen else None for s in self.slots],
            "kwargs": {k: g.generate(rng, size) for k, g in self.kwargs.items()},
        }

    def scaled(self, rng: random.Random, n: int) -> dict[str, Any]:
        def one(gen: Generator, scale: bool | None) -> Any:
            if scale is True or (scale is None and gen.scalable):
                return gen.scaled(rng, n)
            return gen.generate(rng, 0.3)

        return {
            "args": [one(s.gen, s.scale) if s.gen else None for s in self.slots],
            "kwargs": {k: one(g, None) for k, g in self.kwargs.items()},
        }

    def edge_cases(self, limit_per_arg: int = 12) -> list[dict[str, Any]]:
        """Boundary-first cases: vary one argument across its edge values at a time."""
        rng = random.Random(0)
        base_args = []
        edge_lists = []
        for s in self.slots:
            if s.gen is None:
                base_args.append(None)
                edge_lists.append([])
                continue
            edges = s.gen.edge_cases()[:limit_per_arg]
            edge_lists.append(edges)
            base_args.append(edges[0] if edges else s.gen.generate(rng, 0.0))
        base_kwargs = {}
        kw_edges = {}
        for k, g in self.kwargs.items():
            edges = g.edge_cases()[:limit_per_arg]
            kw_edges[k] = edges
            base_kwargs[k] = edges[0] if edges else g.generate(rng, 0.0)

        out: list[dict[str, Any]] = [{"args": list(base_args), "kwargs": dict(base_kwargs)}]
        for i, edges in enumerate(edge_lists):
            for e in edges[1:]:
                args = list(base_args)
                args[i] = e
                out.append({"args": args, "kwargs": dict(base_kwargs)})
        for k, edges in kw_edges.items():
            for e in edges[1:]:
                kwargs = dict(base_kwargs)
                kwargs[k] = e
                out.append({"args": list(base_args), "kwargs": kwargs})
        # Deduplicate while keeping order.
        seen: set[str] = set()
        uniq = []
        for case in out:
            key = repr(case)
            if key not in seen:
                seen.add(key)
                uniq.append(case)
        return uniq

    # -- shrinking --------------------------------------------------------- #
    def shrink(self, case: dict[str, Any]) -> Iterator[dict[str, Any]]:
        args = case["args"]
        kwargs = case["kwargs"]
        for i, slot in enumerate(self.slots):
            if slot.gen is None:
                continue
            for cand in slot.gen.shrink(args[i]):
                new_args = list(args)
                new_args[i] = cand
                yield {"args": new_args, "kwargs": kwargs}
        for k, gen in self.kwargs.items():
            for cand in gen.shrink(kwargs[k]):
                new_kwargs = dict(kwargs)
                new_kwargs[k] = cand
                yield {"args": args, "kwargs": new_kwargs}

    # -- materialization --------------------------------------------------- #
    def materialize(self, case: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
        """Turn a case into the concrete ``(args, kwargs)`` used for the call."""
        raw = case["args"]
        kwargs = dict(case["kwargs"])
        args: list[Any] = []
        for i, slot in enumerate(self.slots):
            if slot.derived is not None:
                ref = slot.derived.get("arg", 0)
                target = kwargs.get(ref) if isinstance(ref, str) else raw[ref]
                try:
                    value: Any = len(target) + int(slot.derived.get("offset", 0))
                except TypeError:
                    value = 0
            else:
                value = raw[i]
            if slot.spread and isinstance(value, (tuple, list)):
                args.extend(value)
            else:
                args.append(value)
        return args, kwargs

    def size_of(self, case: dict[str, Any]) -> int:
        args, kwargs = self.materialize(case)
        return value_size(args) + value_size(kwargs)

    def describe(self) -> dict[str, Any]:
        return {
            "args": [s.gen.describe() if s.gen else dict(s.derived or {}) for s in self.slots],
            "kwargs": {k: g.describe() for k, g in self.kwargs.items()},
        }
