"""Exact, path-addressed diffs between oracle and candidate outputs."""

from __future__ import annotations

import difflib
import pprint
from typing import Any

from counterexample.codec import short_repr
from counterexample.engine.compare import Comparator, canonical_key


def structural_diff(oracle: Any, candidate: Any, eq: Comparator, path: str = "$", limit: int = 20) -> list[dict[str, Any]]:
    """List the paths where ``candidate`` departs from ``oracle``.

    Entries look like ``{"path": "$[2]", "kind": "value", "oracle": "5", "candidate": "4"}``.
    """
    out: list[dict[str, Any]] = []

    def add(p: str, kind: str, o: Any = None, c: Any = None, **extra: Any) -> None:
        if len(out) < limit:
            entry: dict[str, Any] = {"path": p, "kind": kind}
            if o is not None or kind in ("value", "type"):
                entry["oracle"] = short_repr(o, 160)
            if c is not None or kind in ("value", "type"):
                entry["candidate"] = short_repr(c, 160)
            entry.update(extra)
            out.append(entry)

    def walk(o: Any, c: Any, p: str) -> None:
        if len(out) >= limit or eq(o, c):
            return
        if isinstance(o, (list, tuple)) and isinstance(c, (list, tuple)):
            if len(o) != len(c) and len(out) < limit:
                out.append({"path": p, "kind": "length", "oracle": len(o), "candidate": len(c)})
            for i in range(min(len(o), len(c))):
                walk(o[i], c[i], f"{p}[{i}]")
            for i in range(len(c), len(o)):
                add(f"{p}[{i}]", "missing", o[i], None)
            for i in range(len(o), len(c)):
                add(f"{p}[{i}]", "extra", None, c[i])
            return
        if isinstance(o, dict) and isinstance(c, dict):
            ck = {canonical_key(k): k for k in c}
            for k, v in o.items():
                key = canonical_key(k)
                if key not in ck:
                    add(f"{p}[{short_repr(k, 40)}]", "missing_key", v, None)
                else:
                    walk(v, c[ck[key]], f"{p}[{short_repr(k, 40)}]")
            ok = {canonical_key(k) for k in o}
            for k, v in c.items():
                if canonical_key(k) not in ok:
                    add(f"{p}[{short_repr(k, 40)}]", "extra_key", None, v)
            return
        if isinstance(o, (set, frozenset)) and isinstance(c, (set, frozenset)):
            ok = {canonical_key(x): x for x in o}
            ck = {canonical_key(x): x for x in c}
            missing = [ok[k] for k in ok.keys() - ck.keys()]
            extra = [ck[k] for k in ck.keys() - ok.keys()]
            if missing:
                add(p, "missing_elements", missing, None)
            if extra:
                add(p, "extra_elements", None, extra)
            return
        if type(o) is not type(c) and not (isinstance(o, (int, float)) and isinstance(c, (int, float))):
            add(p, "type", o, c, oracle_type=type(o).__name__, candidate_type=type(c).__name__)
            return
        add(p, "value", o, c)

    walk(oracle, candidate, path)
    if not out and not eq(oracle, candidate):
        add(path, "value", oracle, candidate)
    return out


def text_diff(oracle: Any, candidate: Any, limit: int = 3000) -> str:
    a = pprint.pformat(oracle, width=72).splitlines()
    b = pprint.pformat(candidate, width=72).splitlines()
    text = "\n".join(difflib.unified_diff(a, b, fromfile="oracle", tofile="candidate", lineterm=""))
    return text if len(text) <= limit else text[:limit] + "\n... (truncated)"


def summarize(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return "outputs differ"
    e = entries[0]
    kind = e["kind"]
    if kind == "length":
        return f"length differs at {e['path']}: oracle has {e['oracle']} items, candidate has {e['candidate']}"
    if kind in ("missing", "missing_key", "missing_elements"):
        return f"candidate is missing {e.get('oracle')} at {e['path']}"
    if kind in ("extra", "extra_key", "extra_elements"):
        return f"candidate has unexpected {e.get('candidate')} at {e['path']}"
    if kind == "type":
        return f"type differs at {e['path']}: oracle {e.get('oracle_type')} {e['oracle']} vs candidate {e.get('candidate_type')} {e['candidate']}"
    return f"first divergence at {e['path']}: oracle={e['oracle']} candidate={e['candidate']}"
