# ⚡ Counterexample

**A Neuro-Symbolic Falsification & Self-Play Engine for Coding Agents**

![Hacktoberfest Friendly](https://img.shields.io/badge/Hacktoberfest-friendly-ff6f00)
![MCP Server & Skill](https://img.shields.io/badge/MCP-server%20%26%20skill-6e40c9)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776ab)
![C++17 / C++20](https://img.shields.io/badge/C%2B%2B-17%20%2F%2020-00599c)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

> "All unit tests passed" is the biggest lie in AI software engineering. When an LLM writes code and
> writes its own unit tests in the same context window, both are sampled from the exact same
> probability distribution, so the tests inherit the exact same blind spots as the bugs.

**Counterexample** is an Agent Skill and Model Context Protocol (MCP) server that replaces
single-shot LLM guessing with an AlphaZero-style self-play and falsification loop. Instead of
trusting hand-written unit tests, coding agents use Counterexample to synthesize adversarial input
distributions, pit optimized candidate code against a ground-truth oracle, and deterministically
shrink failing inputs into minimal counterexamples that fit cleanly inside a Transformer's
attention horizon.

---

## Why This Exists: The Neuro-Symbolic Pitch

### 1. Breaking Epistemic Collapse (the Shared Blindspot Paradox)

When a human developer writes a feature and a QA engineer writes tests, you have two independent
mental models. When an AI agent writes both implementation and tests, it suffers from *epistemic
collapse*: if the model forgets that a graph can contain disconnected components or negative
weights while coding, it will also forget to test for them. Counterexample breaks the agent out of
its own probability distribution using composable adversarial generators: boundary values,
`INT_MIN`/`INT_MAX`, duplicates, NaN/Inf, tricky Unicode, disconnected graphs, negative cycles,
nested/touching intervals, and singular matrices are on by default.

### 2. Delta-Shrinking as Context Compression for Attention

Traditional fuzzers dump 1,000-line crash logs or massive 500-node graphs. If you feed a
500-element failing array into an LLM context window, its attention degrades into noise: it cannot
mentally trace state transitions across hundreds of steps. Counterexample uses a deterministic
binary-search **Delta Shrinker** to crush a 500-element failure down to the smallest possible
failing input (e.g. `[0, -1, 0]`), turning a chaotic runtime bug into a 3-step trace an LLM can
solve on the first try.

### 3. Test-Time Compute & Self-Play for Code

Rather than spitting out tokens blindfolded, agents use Counterexample as a deterministic physics
engine for code. An agent can attempt an aggressive O(N log N) dynamic programming or greedy
optimization, falsify it against a dead-simple O(N²) brute-force oracle in 200 ms, inspect the
shrunk counterexample, fix its state transition, and repeat until verified across 10,000 runs.

```
 ┌──────────────────────┐  1. Submit code   ┌───────────────────────────────┐   ┌──────────────────────────────┐
 │   AI CODING AGENT    │ ────────────────► │   2. DIFFERENTIAL ARENA       │   │   3. DELTA SHRINKER ENGINE   │
 │ Claude Code / Cursor │                   │  Oracle            Candidate  │──►│ Binary-search input reduction│
 │ Windsurf / LangGraph │                   │ (git HEAD/Naive)  (Optimized) │   │        (N=500 → N=3)         │
 └──────────────────────┘                   │   10,000+ adversarial runs    │   │   Minimal input: [0, -1, 0]  │
            ▲                               │        Divergence!            │   └──────────────┬───────────────┘
            │                               └───────────────────────────────┘                  │
            └──────────── 4. Return minimal counterexample + state diff to agent context ◄─────┘
```
*Figure 1: Neuro-symbolic falsification & delta-shrinking self-play loop (MCP server architecture).*

---

## Exposed Agent Tools (MCP & CLI Interface)

| Tool | Input parameters | Output returned to agent |
| --- | --- | --- |
| `run_differential_arena` | `oracle_code`, `candidate_code`, `generator_spec`, `num_runs`, `timeout_ms` | Pass rate, speedup multiplier (`24.6x faster`), or the minimal shrunk counterexample + exact diff between Oracle and Candidate outputs. |
| `extract_git_oracle` | `file_path`, `symbol_name`, `git_ref` | Automatically extracts the pre-refactor implementation from git `HEAD` (or any commit) to act as a zero-effort ground-truth oracle. |
| `profile_empirical_complexity` | `candidate_code`, `generator_spec`, `scale_steps` | Runs inputs at N = 10¹, 10², 10³, 10⁴, fits execution time & memory growth curves, and flags silent O(N²) regressions before production. |
| `lock_regression_test` | `counterexample_payload`, `test_file_path`, `framework` | Converts captured minimal counterexamples into permanent `pytest`, `jest` (or `vitest`), or `gtest` unit tests. |

Optional parameters (all tools): `oracle_entrypoint` / `candidate_entrypoint` / `entrypoint`,
`language` (`auto`, `python`, `cpp`, `fastapi`; oracle and candidate may differ), `comparator`
(`auto`, `exact`, `strict`, `approx`, `unordered`, `unordered_deep`, `set`), `oracle_errors`
(`discard` | `fail`), `seed`, `expected_complexity`, `module` (import path for locked tests).

### What a falsification looks like

Real output (abridged) for [`examples/max_subarray`](examples/max_subarray), a Kadane
implementation that returns `0` for all-negative arrays, against a brute-force oracle:

```jsonc
{
  "status": "FALSIFIED",
  "summary": "FALSIFIED: 329/3000 valid inputs diverged. Minimal counterexample: max_subarray([-1]) -> first divergence at $: oracle=-1 candidate=0.",
  "shrunk_counterexample": {"call": "max_subarray([-1])", "args": ["[-1]"], "kwargs": {}, "size": 1},
  "oracle_output": "-1",
  "candidate_output": "0",
  "diff": [{"path": "$", "kind": "value", "oracle": "-1", "candidate": "0"}],
  "candidate_trace": {"events": [
    {"line": 3, "code": "best = cur = 0", "changed": {"nums": "[-1]"}},
    {"line": 4, "code": "for x in nums:", "changed": {"best": "0", "cur": "0"}},
    {"line": 5, "code": "cur = max(0, cur + x)", "changed": {"x": "-1"}}
  ]},
  "timing": {"oracle_ms_total": 55.367, "candidate_ms_total": 7.768, "speedup": 7.13, "speedup_text": "7.1x faster"},
  "repro": "assert max_subarray([-1]) == -1",
  "counterexample_payload": {"schema": "counterexample/v1", "function": "max_subarray", "args": [[-1]],
                             "expected_kind": "return", "expected": -1, "failure_kind": "mismatch"}
}
```

Other bundled examples (`python examples/run_examples.py`) find, among others: Dijkstra on
negative weights, shrunk to `shortest(3, [[0, 2, 0], [0, 1, 1], [1, 2, -2]], 0)`; an interval
merge that misses touching ranges, shrunk to `merge([[0, 0], [0, 0]])`; a C++ LIS using
`upper_bound`, shrunk to `[0, 0]`; and a FastAPI rewrite whose coupon threshold drifted from
`>=` to `>`, shrunk to a single `{"prices": [100], "coupon": "HALF"}` request.

---

## Quickstart

### 1. Clone, install & configure MCP

```bash
git clone https://github.com/VasudevCodeReaper/CounterExample.git
cd CounterExample && pip install -e .
```

Add to `claude_desktop_config.json` or `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "counterexample": {
      "command": "python",
      "args": ["-m", "counterexample.mcp_server"],
      "env": {
        "SANDBOX_TIMEOUT_MS": "2000",
        "DEFAULT_ARENA_RUNS": "5000"
      }
    }
  }
}
```

Claude Code: `claude mcp add counterexample -- python -m counterexample.mcp_server`
(this repository also ships a ready-made [`.mcp.json`](.mcp.json)).

Optional extras: `pip install -e ".[fastapi]"` for endpoint testing, `pip install -e ".[dev]"` for
the test suite. C++ support needs `g++` or `clang++` on `PATH` (or set `$CXX`; `pip install ziglang`
works as a zero-setup fallback on any OS).

### 2. Portable skill setup

Copy `SKILL.md` into your repository's `.agents/skills/counterexample/` or `.claude/skills/`
directory so your agent knows when to trigger falsification loops:

```bash
counterexample install-skill --target both --dir /path/to/your/repo
```

### 3. Try it from the command line

```bash
counterexample arena --oracle examples/max_subarray/oracle.py --candidate examples/max_subarray/candidate_buggy.py \
                     --spec '{"type": "list", "elements": {"type": "int", "min": -50, "max": 50}, "min_len": 1}'
counterexample profile --candidate examples/max_subarray/candidate_fixed.py --expected "O(N)"
counterexample oracle src/scheduler.py merge_intervals --ref HEAD~1 -o /tmp/oracle.py
```

Exit codes make it CI-friendly: `0` VERIFIED/OK, `1` FALSIFIED/FLAGGED, `2` error.

---

## Repository Structure

```
counterexample/
├── SKILL.md                    # Agent skill definition
├── README.md                   # Project documentation
├── pyproject.toml              # Package & CLI entrypoints
├── counterexample/
│   ├── mcp_server.py           # FastMCP agent bridge (mcp 1.x FastMCP / 2.x MCPServer)
│   ├── cli.py                  # `counterexample` CLI
│   ├── codec.py                # Lossless value transport (tuples, sets, NaN/Inf, bytes...)
│   ├── engine/
│   │   ├── arena.py            # Parallel sandbox runner
│   │   ├── shrinker.py         # Delta-debugging reducer
│   │   ├── profiler.py         # Empirical Big-O fitter
│   │   ├── git_oracle.py       # Git AST code extractor
│   │   ├── regression.py       # pytest / jest / gtest test locking
│   │   ├── compare.py          # Output comparators
│   │   └── diff.py             # Path-addressed output diffs
│   ├── generators/             # Composable fuzz generators
│   │   ├── primitives.py       # Ints, NaN/Inf, Unicode, lists, dicts, permutations
│   │   ├── graphs.py           # DAGs, trees, negative cycles
│   │   ├── intervals.py        # Nested/overlapping ranges
│   │   ├── matrices.py         # Sparse & singular grids
│   │   ├── http.py             # HTTP requests for endpoint testing
│   │   └── infer.py            # Specs from type hints / C++ signatures
│   └── adapters/               # Execution harnesses
│       ├── python_runner.py
│       ├── cpp_runner.py
│       └── fastapi_runner.py
├── examples/                   # Runnable falsification demos
├── tests/                      # pytest suite
├── .claude/skills/, .agents/skills/   # SKILL.md installed for agents working on this repo
├── .mcp.json                   # Project MCP config for Claude Code
├── .github/workflows/          # CI (tests, examples, Docker smoke test) and release
├── Dockerfile                  # Deployable MCP server (streamable HTTP)
└── docker-compose.yml
```

---

## Generator specs

`generator_spec` is plain JSON (full reference: `counterexample generators`, or the MCP resource
`counterexample://generators`). Omit it and Counterexample infers one from Python type hints or the
C++ signature.

```jsonc
// shorthand strings
"list[int(-50,50)]"
// one spec per positional argument; derived lengths; splatted graphs
{"args": [{"type": "len_of", "arg": 1}, {"type": "list", "elements": "int", "max_len": 40}]}
{"args": [{"type": "graph", "directed": true, "weighted": true, "negative_cycles": true, "spread": true},
          {"type": "int", "min": 0, "max": 0}]}
// intervals, matrices, grids, Unicode
{"type": "intervals", "mode": "mixed", "allow_empty": true}
{"type": "matrix", "square": true, "kind": "singular"}
{"type": "grid", "cells": [".", "#"], "format": "strings"}
{"type": "str", "alphabet": "unicode", "max_len": 12}
```

Boundary cases are always run first (`0`, `-1`, `INT_MIN`/`INT_MAX`, empty and single-element
collections, duplicates, isolated nodes, touching intervals, 0×N matrices...), then random inputs
whose sizes ramp up across the run. Generation is seeded and deterministic.

## How the sandbox works

* Every implementation runs in its own long-lived child process (`python -I` in a throwaway
  directory; compiled binaries for C++), talking JSON lines. Thousands of calls cost one process
  start, so 10,000 small runs finish in well under a second.
* Python entrypoints may be synchronous or `async def`; awaitable results are resolved inside the
  worker before the oracle and candidate outputs are compared.
* Per-call timeouts kill and respawn the process; segfaults and `os._exit` become `candidate_crash`;
  `print` output never corrupts the protocol.
* Work is sharded over parallel oracle/candidate process pairs (`COUNTEREXAMPLE_MAX_WORKERS`).
* Oracle and candidate may be written in different languages: a Python brute force can falsify an
  optimized C++ `class Solution`.
* Memory is capped with `RLIMIT_AS` on POSIX (`SANDBOX_MEMORY_MB`). The sandbox protects the agent
  session from crashes and hangs; it is not a security boundary against hostile code. Run untrusted
  code inside the Docker image.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `SANDBOX_TIMEOUT_MS` | `2000` | Per-call timeout for oracle/candidate invocations |
| `DEFAULT_ARENA_RUNS` | `5000` | `num_runs` when the agent does not pass one |
| `COUNTEREXAMPLE_MAX_WORKERS` | `min(4, cpus/2)` | Parallel oracle/candidate process pairs |
| `SANDBOX_MEMORY_MB` | `1024` | Address-space cap per sandbox process (POSIX) |
| `ARENA_BUDGET_S` | `120` | Wall-clock budget for one arena call |
| `SHRINK_MAX_EVALS` | `5000` | Evaluation budget for the delta shrinker |
| `CXX` / `CXX_STD` / `CXX_FLAGS` | `g++` / `c++17` / – | C++ compiler, standard and extra flags (e.g. `-fsanitize=address,undefined`) |
| `COUNTEREXAMPLE_CACHE` | system temp | Where compiled C++ harnesses are cached |
| `MCP_TRANSPORT` / `HOST` / `PORT` | `stdio` / `127.0.0.1` / `8000` | Server transport for `counterexample-mcp` |
| `COUNTEREXAMPLE_API_TOKEN` | – | Bearer token required on HTTP transports (mandatory when binding a non-loopback host) |
| `COUNTEREXAMPLE_ALLOW_UNAUTHENTICATED` | – | Set to `1` to serve a non-loopback host without a token (isolated networks only) |
| `MCP_ALLOWED_HOSTS` | – | Comma-separated `Host` header allow-list (DNS-rebinding protection), e.g. `mcp.internal:*` |

## Deployment

The MCP server runs over stdio (local agents) or streamable HTTP (remote agents, containers).
Because it executes submitted code, every HTTP request must carry
`Authorization: Bearer $COUNTEREXAMPLE_API_TOKEN`; the server refuses to bind a non-loopback
address without a token. `GET /healthz` is unauthenticated for load balancers.

```bash
export COUNTEREXAMPLE_API_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")

# Local HTTP server (endpoint: http://localhost:8000/mcp)
counterexample-mcp --transport streamable-http --host 0.0.0.0 --port 8000

# Docker (includes g++ and git)
docker build -t counterexample .
docker run --rm -p 8000:8000 -e COUNTEREXAMPLE_API_TOKEN counterexample
# or, hardened (read-only rootfs, no capabilities, pids/memory limits):
docker compose up -d
```

Connect a client, e.g. Claude Code:

```bash
claude mcp add --transport http counterexample http://localhost:8000/mcp --header "Authorization: Bearer $COUNTEREXAMPLE_API_TOKEN"
```

## Development

```bash
pip install -e ".[dev]"
pytest                 # C++ tests run when a compiler is available, FastAPI tests when fastapi is installed
```

See [CONTRIBUTING.md](CONTRIBUTING.md). New generators, language adapters and test-framework
renderers are great first contributions (Hacktoberfest friendly).

## License

MIT. See [LICENSE](LICENSE).
