# Contributing to Counterexample

Thanks for helping agents stop trusting their own unit tests. Counterexample is
Hacktoberfest friendly: small, well-scoped pull requests are very welcome.

## Setup

```bash
git clone https://github.com/VasudevCodeReaper/CounterExample.git
cd CounterExample
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

C++ tests run when `g++`/`clang++` is on `PATH` (or `$CXX` is set, or `pip install ziglang`).
FastAPI tests run when `fastapi` and `httpx` are installed (included in `[dev]`).

## Good first contributions

- **Generators** (`counterexample/generators/`): subclass `Generator`, decorate it with
  `@register("name")`, and implement `generate`, `edge_cases`, `shrink`, `validate` (and `scaled`
  for collections). Shrink candidates must be valid under `validate` and should come
  most-aggressive first. Add your spec to `SPECS` in `tests/test_generators.py`, which checks
  validity of generated, boundary and shrunk values automatically, and document it in
  `GENERATOR_REFERENCE`.
- **Language adapters** (`counterexample/adapters/`): implement `prepare`, `spawn`, `infer_spec`,
  `describe` and speak the JSON-lines protocol documented in `_python_worker.py`
  (`setup` / `call` / `profile` / `exit`, replies `{"id", "ok", "out", "ns"}`).
- **Test renderers** (`counterexample/engine/regression.py`): new frameworks for
  `lock_regression_test`.
- **Examples** (`examples/`): a brute-force oracle, a subtly wrong optimized candidate and a
  `spec.json`. Register it in `examples/run_examples.py`.

## Ground rules

- Keep the engine deterministic: every random choice flows from the caller's `seed`.
- Tool results must stay JSON-serializable (no NaN, sets or tuples at the top level); use
  `codec.encode` for values and `codec.py_literal` for display.
- `adapters/_python_worker.py` must not import anything from `counterexample` (it runs under
  `python -I`). Its private copy of `encode`/`decode` mirrors `codec.py`; change both together.
- Add or update tests for every behavior change; run `pytest` before opening a PR.
