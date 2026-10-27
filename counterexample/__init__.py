"""Counterexample: a neuro-symbolic falsification & self-play engine for coding agents.

The public Python API mirrors the four MCP tools exposed by
:mod:`counterexample.mcp_server`:

* :func:`run_differential_arena`       - falsify a candidate against an oracle.
* :func:`extract_git_oracle`           - pull a pre-refactor oracle out of git.
* :func:`profile_empirical_complexity` - fit empirical Big-O growth curves.
* :func:`lock_regression_test`         - turn a counterexample into a unit test.
"""

from __future__ import annotations

from typing import Any

__version__ = "0.1.0"

_LAZY = {
    "run_differential_arena": "counterexample.engine.arena",
    "extract_git_oracle": "counterexample.engine.git_oracle",
    "profile_empirical_complexity": "counterexample.engine.profiler",
    "lock_regression_test": "counterexample.engine.regression",
}

__all__ = ["__version__", *_LAZY]


def __getattr__(name: str) -> Any:
    # Imported lazily so `import counterexample.generators` stays lightweight.
    if name in _LAZY:
        import importlib

        return getattr(importlib.import_module(_LAZY[name]), name)
    raise AttributeError(f"module 'counterexample' has no attribute {name!r}")
