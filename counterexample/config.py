"""Runtime configuration, read from environment variables.

The MCP client config (``claude_desktop_config.json`` / ``.cursor/mcp.json``)
sets these through its ``env`` block::

    "env": {"SANDBOX_TIMEOUT_MS": "2000", "DEFAULT_ARENA_RUNS": "5000"}
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # Per-call timeout for a single oracle/candidate invocation.
    sandbox_timeout_ms: int
    # Number of adversarial inputs when the caller does not pass num_runs.
    default_arena_runs: int
    # Upper bound on worker process pairs used by the parallel arena.
    max_workers: int
    # Address-space cap for sandbox processes (POSIX only, 0 disables).
    sandbox_memory_mb: int
    # Wall-clock budget for one whole arena call, in seconds.
    arena_budget_s: float
    # Maximum predicate evaluations the delta shrinker may spend.
    shrink_max_evals: int
    # C++ compiler used by the cpp adapter.
    cxx: str
    cxx_std: str


def get_settings() -> Settings:
    cpu = os.cpu_count() or 2
    return Settings(
        sandbox_timeout_ms=_env_int("SANDBOX_TIMEOUT_MS", 2000),
        default_arena_runs=_env_int("DEFAULT_ARENA_RUNS", 5000),
        max_workers=max(1, _env_int("COUNTEREXAMPLE_MAX_WORKERS", min(4, max(1, cpu // 2)))),
        sandbox_memory_mb=_env_int("SANDBOX_MEMORY_MB", 1024),
        arena_budget_s=_env_float("ARENA_BUDGET_S", 120.0),
        shrink_max_evals=_env_int("SHRINK_MAX_EVALS", 5000),
        cxx=os.environ.get("CXX", "g++"),
        cxx_std=os.environ.get("CXX_STD", "c++17"),
    )
