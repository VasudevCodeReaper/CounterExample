---
name: counterexample
description: Falsify code changes with independent oracles, adversarial generators, metamorphic properties, delta-shrinking, and empirical complexity checks. Use for behavior-preserving refactors, algorithms, stateful logic, boundary bugs, or bug fixes that need stronger evidence than unit tests.
---

# Counterexample: Falsification & Self-Play Protocol

Never trust hand-written unit tests alone when implementing non-trivial logic, algorithmic
optimizations, or code refactors. Standard unit tests written in the same context window suffer
from shared blindspots. Use the Counterexample engine to empirically falsify your code and
compress failing inputs into minimal counterexamples.

The engine is available as MCP tools (`run_differential_arena`, `run_property_arena`,
`extract_git_oracle`, `profile_empirical_complexity`, `lock_regression_test`) and as the
`counterexample` CLI (`arena`, `property`, `oracle`, `profile`, `lock`).

## When to Trigger This Skill

1. **Refactoring Existing Code**: whenever modifying, cleaning up, or migrating an existing function
   or endpoint whose external behavior must remain identical.
2. **Algorithmic Optimization**: whenever replacing a slow O(N²) or brute-force implementation with
   an optimized O(N log N), O(N), greedy, or dynamic programming solution.
3. **Complex Data Structures & State Transitions**: whenever writing logic involving intervals,
   graphs, trees, concurrency, parsers, or boundary arithmetic.
4. **Mystery Bug Reproduction**: whenever a user reports an edge-case bug that is not caught by
   existing unit tests.

## Step-by-Step Execution Workflow (Self-Play Loop)

### STEP 01: Establish the Ground-Truth Oracle

Differential falsification needs a source of truth. Choose one of two strategies:

- **Case A: Refactoring Existing Code.** Call
  `extract_git_oracle(file_path, symbol_name, git_ref="HEAD")` to retrieve the untouched prior
  version of the function as your Oracle. It returns `oracle_code` (the symbol plus every import,
  helper and constant it depends on) and the `entrypoint` to pass as `oracle_entrypoint`.
  For refactors, pass `oracle_errors="fail"`: raised exceptions are part of the contract.
- **Case B: Writing New Optimized Code.** Write a dead-simple, zero-optimization Brute-Force
  Oracle alongside your optimized candidate. Keep the Oracle under 15–20 lines of obvious logic
  (e.g., exhaustive search or naive iteration) that prioritizes 100% mathematical clarity over
  time complexity.

The Oracle must be independent of the Candidate: never derive it by simplifying the candidate,
and never edit the Oracle to make a failing run pass.

### STEP 01B: Add a Property When It Adds Independent Evidence

When no practical oracle exists, or when a high-value invariant supplements differential testing,
call `run_property_arena(candidate_code, property_code, generator_spec)`. The property source must
define `property(result, *args, **kwargs) -> bool`. It can be async and may call the candidate on a
transformed input to express a metamorphic relation:

```python
# candidate defines solve(values)
def property(result, values):
    return result == solve(list(reversed(values)))  # order-invariance relation
```

Use properties that follow directly from the contract: ordering plus element preservation,
conservation laws, bounds, idempotence, reversibility, or invariance under a valid transformation.
Do not recreate the candidate's algorithm inside `property`. Property mode checks only the stated
invariant; a `PROPERTY VERIFIED` result is not full behavioral equivalence. Prefer an independent
oracle whenever one is feasible.

For stateful, time-dependent, random, networked, or database-backed code, first isolate a
deterministic pure adapter. Do not treat an in-process property check as an integration test.

### STEP 02: Configure the Adversarial Generator

Select or compose an input generator spec matching the target function's signature (read the
`counterexample://generators` resource, or run `counterexample generators`):

- Include boundary conditions by default: `0`, `-1`, empty collections, single-element
  collections, duplicate values, `INT_MIN` / `INT_MAX`, and skewed distributions. The engine
  enumerates these first automatically; do not narrow ranges so far that they disappear.
- Keep initial input sizes small-to-medium (N ≤ 50) when comparing against an exponential or
  O(N²) Brute-Force Oracle so thousands of runs finish in under 2 seconds.
- Match the real domain: if the function takes a graph, use `graph`/`dag`/`tree` (with
  disconnected components, negative weights, negative cycles); intervals → `intervals`
  (nested, overlapping, touching, zero-length); grids/matrices → `matrix`/`grid` (sparse,
  singular, 0×N).
- Omitting `generator_spec` infers it from Python type hints or the C++ signature.

```json
{"args": [{"type": "list", "elements": {"type": "int", "min": -50, "max": 50}, "max_len": 40},
          {"type": "int", "min": 0, "max": 10}]}
```

### STEP 03: Run the Differential Arena

Invoke `run_differential_arena` with your `oracle_code`, `candidate_code`, and `generator_spec`.

- If `status == "FALSIFIED"`: **do not guess at fixes blindly.** Inspect the
  `shrunk_counterexample` returned by the Delta Shrinker. Because the shrinker reduces the
  failing input to its minimal reproduction state, trace the candidate code step-by-step on that
  exact minimal input (`candidate_trace` lists every executed line and the locals it changed;
  `diff` gives the exact path where outputs diverge). Identify the exact line where Candidate
  state diverges from Oracle state, patch the candidate code, and re-run the arena **with the same
  `seed`**.
- If `status == "VERIFIED"`: note `pass_rate` and `timing.speedup_text`, then continue to STEP 04.
- If `status == "INCONCLUSIVE"`: the oracle rejected every input; read `discarded_samples` and fix
  the generator to respect the oracle's preconditions.
- If `status == "ERROR"`: fix the reported load/compile/spec problem (`details` holds the
  traceback or compiler output) and re-run.

Repeat until VERIFIED.

### STEP 04: Verify at Scale & Profile Complexity

1. Re-run the arena with `num_runs=10000` (and widened ranges/sizes where the oracle stays fast)
   for the final verdict.
2. Call `profile_empirical_complexity(candidate_code, generator_spec, scale_steps=[10, 100, 1000, 10000],
   expected_complexity="O(N log N)")`. A `FLAGGED` result (`SUPERLINEAR`, `REGRESSION`, `TIMEOUT`)
   means the optimization is not what it claims; treat it as a failure.
3. Run at least two holdout seeds with the same domain after the repair passes the reproduction
   seed. Widen ranges or sizes where the oracle remains tractable. One deterministic seed is a
   reproducible diagnosis, not sufficient final coverage.

### STEP 05: Lock the Regression

For every *differential* counterexample found during the loop, call
`lock_regression_test(counterexample_payload, test_file_path, framework)` with `pytest`, `jest`,
`vitest` or `gtest`. Pass `module` (import path / header) when the definition cannot be found
automatically. Run the generated test (`run_command`) to confirm it passes against the fixed code.
For property failures, preserve `property_counterexample` and encode that input plus the explicit
property in the target test suite.

## Rules

- Never report "all tests pass" for non-trivial logic without a differential `VERIFIED` arena run,
  or clearly state that only named properties were checked when an oracle is unavailable.
- Never weaken the oracle, comparator, property, or generator to make a failure disappear. Use
  `comparator="unordered"`/`"set"`/`"approx"` only when the specification genuinely allows it.
- Fix the root cause on the minimal input; do not special-case the counterexample's values.
- Report the evidence mode, oracle provenance or exact property, seeds, final pass rate/run count,
  speedup, and empirical complexity to the user.
