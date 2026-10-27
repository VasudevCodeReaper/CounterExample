"""``counterexample`` command-line interface (also usable in CI).

    counterexample arena --oracle brute.py --candidate fast.py --spec '"list[int(-9,9)]"'
    counterexample oracle src/algo.py merge_intervals --ref HEAD~1 -o oracle.py
    counterexample profile --candidate fast.py --expected "O(N log N)"
    counterexample lock payload.json tests/test_regressions.py --framework pytest
    counterexample serve --transport streamable-http --port 8000
    counterexample install-skill --target claude

Exit codes: 0 = VERIFIED / OK / LOCKED, 1 = FALSIFIED / FLAGGED, 2 = error.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from counterexample import __version__


def _read_code(value: str) -> str:
    """Accept a file path, '-' for stdin, or literal source/URL."""
    if value == "-":
        return sys.stdin.read()
    path = Path(value)
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return value


def _read_json(value: str | None) -> Any:
    if value is None:
        return None
    if value.startswith("@"):
        value = Path(value[1:]).read_text(encoding="utf-8")
    elif Path(value).is_file():
        value = Path(value).read_text(encoding="utf-8")
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value  # shorthand such as list[int]


def _emit(result: dict[str, Any], as_json: bool, keys: list[str]) -> None:
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    print(result.get("summary") or result.get("status"))
    for key in keys:
        if key in result and result[key] not in (None, [], {}):
            val = result[key]
            text = val if isinstance(val, str) else json.dumps(val, indent=2, ensure_ascii=False)
            print(f"\n{key}:\n{text}")
    if result.get("error"):
        print(f"\nerror: {result['error']}", file=sys.stderr)
        details = result.get("details") or {}
        if details.get("compiler_output"):
            print(details["compiler_output"], file=sys.stderr)
        elif details.get("tb"):
            print(details["tb"], file=sys.stderr)


def _cmd_arena(a: argparse.Namespace) -> int:
    from counterexample.engine.arena import run_differential_arena

    result = run_differential_arena(
        _read_code(a.oracle),
        _read_code(a.candidate),
        _read_json(a.spec),
        a.runs,
        a.timeout_ms,
        oracle_entrypoint=a.oracle_entry,
        candidate_entrypoint=a.candidate_entry,
        language=a.language,
        comparator=a.comparator,
        oracle_errors=a.oracle_errors,
        seed=a.seed,
        workers=a.workers,
    )
    if a.save_payload and result.get("counterexample_payload"):
        Path(a.save_payload).write_text(json.dumps(result["counterexample_payload"], indent=2), encoding="utf-8")
    _emit(result, a.json, ["shrunk_counterexample", "diff_summary", "candidate_error", "repro", "timing", "warnings"])
    return {"VERIFIED": 0, "FALSIFIED": 1}.get(result["status"], 2)


def _cmd_oracle(a: argparse.Namespace) -> int:
    from counterexample.engine.git_oracle import extract_git_oracle

    result = extract_git_oracle(a.file, a.symbol, a.ref, repo_path=a.repo)
    if result["status"] == "OK" and a.output:
        Path(a.output).write_text(result["oracle_code"], encoding="utf-8")
    if a.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif result["status"] == "OK":
        if not a.output:
            print(result["oracle_code"])
        print(
            f"# {result['symbol']} @ {result['git_ref']} ({result['commit'][:10]}), entrypoint={result['entrypoint']}, "
            f"changed_since_ref={result.get('changed_since_ref')}",
            file=sys.stderr,
        )
    else:
        print(f"error: {result['error']}", file=sys.stderr)
        if result.get("available_symbols"):
            print("available: " + ", ".join(result["available_symbols"]), file=sys.stderr)
    return 0 if result["status"] == "OK" else 2


def _cmd_profile(a: argparse.Namespace) -> int:
    from counterexample.engine.profiler import profile_empirical_complexity

    steps = [int(s) for s in a.steps.split(",")] if a.steps else None
    result = profile_empirical_complexity(
        _read_code(a.candidate),
        _read_json(a.spec),
        steps,
        entrypoint=a.entry,
        language=a.language,
        expected_complexity=a.expected,
        repeats=a.repeats,
    )
    if a.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(result.get("summary") or result.get("error"))
        for row in result.get("steps", []):
            mem = f"  peak {row['peak_memory_kb']:.1f} KB" if row.get("peak_memory_kb") is not None else ""
            t = f"{row['time_ms_median']:.4f} ms" if row.get("time_ms_median") is not None else row.get("status")
            print(f"  N={row['n']:>8}  {t}{mem}")
        if result.get("projected_time"):
            print("  projected: " + ", ".join(f"{k}: {v}" for k, v in result["projected_time"].items()))
    return {"OK": 0, "FLAGGED": 1}.get(result["status"], 2)


def _cmd_lock(a: argparse.Namespace) -> int:
    from counterexample.engine.regression import lock_regression_test

    payload = _read_json(a.payload)
    result = lock_regression_test(payload, a.test_file, a.framework, module=a.module, import_line=a.import_line, function_name=a.function)
    if a.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"{result['status']}: {result.get('test_name', '')} -> {result.get('test_file_path', result.get('error'))}")
        for w in result.get("warnings", []):
            print(f"warning: {w}", file=sys.stderr)
        if result.get("run_command"):
            print(f"run: {result['run_command']}")
    return 0 if result["status"] in ("LOCKED", "ALREADY_LOCKED") else 2


def _cmd_serve(a: argparse.Namespace) -> int:
    from counterexample.mcp_server import main as serve

    serve(["--transport", a.transport, "--host", a.host, "--port", str(a.port)])
    return 0


def _cmd_install_skill(a: argparse.Namespace) -> int:
    from counterexample.resources import skill_path

    root = Path(a.dir).resolve()
    targets = {"claude": [".claude/skills/counterexample"], "agents": [".agents/skills/counterexample"], "both": [".claude/skills/counterexample", ".agents/skills/counterexample"]}[a.target]
    src = skill_path()
    for t in targets:
        dest = root / t / "SKILL.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        print(f"installed {dest}")
    return 0


def _cmd_generators(a: argparse.Namespace) -> int:
    from counterexample.generators import GENERATOR_REFERENCE

    print(GENERATOR_REFERENCE)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="counterexample", description="Neuro-symbolic falsification & self-play engine for coding agents.")
    p.add_argument("--version", action="version", version=f"counterexample {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("arena", help="falsify a candidate against an oracle (run_differential_arena)")
    s.add_argument("--oracle", required=True, help="oracle source file, '-' for stdin, or literal code/URL")
    s.add_argument("--candidate", required=True, help="candidate source file or literal code/URL")
    s.add_argument("--spec", help="generator_spec as JSON, shorthand, a file path or @file")
    s.add_argument("--runs", type=int, default=None)
    s.add_argument("--timeout-ms", type=int, default=None)
    s.add_argument("--oracle-entry", default=None)
    s.add_argument("--candidate-entry", default=None)
    s.add_argument("--language", default="auto")
    s.add_argument("--comparator", default="auto")
    s.add_argument("--oracle-errors", default="discard", choices=["discard", "fail"])
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--workers", type=int, default=None)
    s.add_argument("--save-payload", help="write counterexample_payload JSON here when FALSIFIED")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=_cmd_arena)

    s = sub.add_parser("oracle", help="extract a pre-refactor oracle from git (extract_git_oracle)")
    s.add_argument("file")
    s.add_argument("symbol")
    s.add_argument("--ref", default="HEAD")
    s.add_argument("--repo", default=None)
    s.add_argument("-o", "--output", default=None)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=_cmd_oracle)

    s = sub.add_parser("profile", help="fit empirical Big-O curves (profile_empirical_complexity)")
    s.add_argument("--candidate", required=True)
    s.add_argument("--spec", default=None)
    s.add_argument("--steps", default=None, help="comma separated N values, default 10,100,1000,10000")
    s.add_argument("--entry", default=None)
    s.add_argument("--language", default="auto")
    s.add_argument("--expected", default=None, help='e.g. "O(N log N)"')
    s.add_argument("--repeats", type=int, default=5)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=_cmd_profile)

    s = sub.add_parser("lock", help="turn a counterexample payload into a unit test (lock_regression_test)")
    s.add_argument("payload", help="payload JSON, file path or @file (as saved by `arena --save-payload`)")
    s.add_argument("test_file")
    s.add_argument("--framework", default="pytest", choices=["pytest", "jest", "vitest", "gtest"])
    s.add_argument("--module", default=None)
    s.add_argument("--import-line", default=None)
    s.add_argument("--function", default=None)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=_cmd_lock)

    s = sub.add_parser("serve", help="run the MCP server")
    s.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(func=_cmd_serve)

    s = sub.add_parser("install-skill", help="copy SKILL.md into .claude/skills or .agents/skills")
    s.add_argument("--target", choices=["claude", "agents", "both"], default="both")
    s.add_argument("--dir", default=".", help="repository root (default: current directory)")
    s.set_defaults(func=_cmd_install_skill)

    s = sub.add_parser("generators", help="print the generator_spec reference")
    s.set_defaults(func=_cmd_generators)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
