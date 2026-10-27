import json
import subprocess
import sys

from counterexample.cli import main


def test_arena_exit_codes_and_payload(tmp_path, capsys):
    oracle = tmp_path / "oracle.py"
    good = tmp_path / "good.py"
    bad = tmp_path / "bad.py"
    oracle.write_text("def f(a: list[int]) -> int:\n    return sum(a)\n", encoding="utf-8")
    good.write_text("def f(a: list[int]) -> int:\n    t = 0\n    for x in a:\n        t += x\n    return t\n", encoding="utf-8")
    bad.write_text("def f(a: list[int]) -> int:\n    return sum(a[1:])\n", encoding="utf-8")
    assert main(["arena", "--oracle", str(oracle), "--candidate", str(good), "--runs", "200"]) == 0
    payload = tmp_path / "payload.json"
    assert main(["arena", "--oracle", str(oracle), "--candidate", str(bad), "--runs", "200", "--save-payload", str(payload)]) == 1
    out = capsys.readouterr().out
    assert "FALSIFIED" in out and "f([" in out
    data = json.loads(payload.read_text(encoding="utf-8"))
    assert data["schema"] == "counterexample/v1"

    test_file = tmp_path / "test_locked.py"
    assert main(["lock", str(payload), str(test_file), "--module", "mymod"]) == 0
    assert "from mymod import f" in test_file.read_text(encoding="utf-8")


def test_arena_spec_shorthand_and_json(tmp_path, capsys):
    code = "def f(x: int) -> int:\n    return x\n"
    assert main(["arena", "--oracle", code, "--candidate", code, "--spec", "int(0,9)", "--runs", "20", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "VERIFIED"


def test_property_cli(tmp_path, capsys):
    candidate = tmp_path / "candidate.py"
    property_file = tmp_path / "property.py"
    candidate.write_text("def f(values: list[int]) -> list[int]:\n    return sorted(set(values))\n", encoding="utf-8")
    property_file.write_text("def property(result, values):\n    return result == sorted(values)\n", encoding="utf-8")

    assert main(["property", "--candidate", str(candidate), "--property", str(property_file), "--runs", "50"]) == 1
    assert "PROPERTY FALSIFIED" in capsys.readouterr().out


def test_arena_cli_import_path_and_fixtures(tmp_path, capsys):
    (tmp_path / "project_mod.py").write_text(
        "import time\ndef f(value: int) -> tuple[int, float]:\n    return value, time.time()\n",
        encoding="utf-8",
    )
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text("from project_mod import f\n", encoding="utf-8")
    fixture = tmp_path / "fixture.py"
    fixture.write_text(
        "import time\n_real = time.time\ndef before_call(args, kwargs):\n    time.time = lambda: 123.0\ndef after_call(args, kwargs, result, error):\n    time.time = _real\n",
        encoding="utf-8",
    )

    assert main(
        [
            "arena",
            "--oracle",
            str(wrapper),
            "--candidate",
            str(wrapper),
            "--oracle-entry",
            "f",
            "--candidate-entry",
            "f",
            "--import-path",
            str(tmp_path),
            "--oracle-fixture",
            str(fixture),
            "--candidate-fixture",
            str(fixture),
            "--spec",
            "int(0,2)",
            "--runs",
            "12",
        ]
    ) == 0
    assert "VERIFIED" in capsys.readouterr().out


def test_install_skill_and_generators(tmp_path, capsys):
    assert main(["install-skill", "--dir", str(tmp_path)]) == 0
    for sub in (".claude/skills/counterexample/SKILL.md", ".agents/skills/counterexample/SKILL.md"):
        text = (tmp_path / sub).read_text(encoding="utf-8")
        assert text.startswith("---\nname: counterexample")
    assert main(["generators"]) == 0
    assert "generator_spec reference" in capsys.readouterr().out


def test_init_configures_major_agent_hosts_without_overwriting_existing_config(tmp_path, capsys):
    (tmp_path / "AGENTS.md").write_text("# Existing project policy\n", encoding="utf-8")
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"other": {"command": "other-mcp"}}}), encoding="utf-8"
    )

    assert main(["init", "--dir", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "Counterexample is ready" in output

    for sub in (
        ".agents/skills/counterexample/SKILL.md",
        ".claude/skills/counterexample/SKILL.md",
        ".github/skills/counterexample/SKILL.md",
        ".cursor/rules/counterexample.mdc",
        ".github/agents/counterexample.agent.md",
        ".cline/skills/counterexample/SKILL.md",
        ".cline/rules/counterexample.md",
    ):
        assert (tmp_path / sub).is_file(), sub

    agents = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert agents.startswith("# Existing project policy")
    assert agents.count("<!-- counterexample:begin -->") == 1
    assert (tmp_path / "CLAUDE.md").is_file()
    assert (tmp_path / "GEMINI.md").is_file()
    assert (tmp_path / ".github/copilot-instructions.md").is_file()

    generic_mcp = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert {"other", "counterexample"} <= set(generic_mcp["mcpServers"])
    assert json.loads((tmp_path / ".cursor/mcp.json").read_text(encoding="utf-8"))["mcpServers"]["counterexample"]
    assert json.loads((tmp_path / ".gemini/settings.json").read_text(encoding="utf-8"))["mcpServers"]["counterexample"]
    assert json.loads((tmp_path / ".vscode/mcp.json").read_text(encoding="utf-8"))["servers"]["counterexample"]["type"] == "stdio"
    assert json.loads((tmp_path / ".continue/mcpServers/counterexample.json").read_text(encoding="utf-8"))["mcpServers"]["counterexample"]
    codex_config = (tmp_path / ".codex/config.toml").read_text(encoding="utf-8")
    assert "[mcp_servers.counterexample]" in codex_config
    assert "# counterexample:begin" in codex_config
    assert "<!-- counterexample:begin -->" not in codex_config

    # The bootstrap is safe to run again and does not duplicate its policy block.
    assert main(["init", "--dir", str(tmp_path)]) == 0
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8").count("<!-- counterexample:begin -->") == 1


def test_init_only_touches_user_agent_configs_when_explicit(tmp_path, monkeypatch, capsys):
    from counterexample import bootstrap

    windsurf_config = tmp_path / "windsurf" / "mcp_config.json"
    cline_config = tmp_path / "cline" / "cline_mcp_settings.json"
    monkeypatch.setattr(bootstrap, "_windsurf_config_path", lambda: windsurf_config)
    monkeypatch.setattr(bootstrap, "_cline_config_path", lambda: cline_config)

    assert main(["init", "--dir", str(tmp_path), "--target", "windsurf"]) == 0
    assert not windsurf_config.exists()
    assert "user-level MCP config" in capsys.readouterr().out

    assert main(["init", "--dir", str(tmp_path), "--target", "windsurf", "--configure-windsurf"]) == 0
    assert json.loads(windsurf_config.read_text(encoding="utf-8"))["mcpServers"]["counterexample"]

    assert main(["init", "--dir", str(tmp_path), "--target", "cline"]) == 0
    assert not cline_config.exists()
    assert "user-level MCP config" in capsys.readouterr().out

    assert main(["init", "--dir", str(tmp_path), "--target", "cline", "--configure-cline"]) == 0
    assert json.loads(cline_config.read_text(encoding="utf-8"))["mcpServers"]["counterexample"]


def test_module_entrypoint_displays_cli_help():
    result = subprocess.run(
        [sys.executable, "-m", "counterexample", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "init" in result.stdout


def test_profile_cli(tmp_path, capsys):
    cand = tmp_path / "c.py"
    cand.write_text("def f(a: list[int]) -> int:\n    return len(a)\n", encoding="utf-8")
    assert main(["profile", "--candidate", str(cand), "--steps", "10,100,1000"]) in (0, 1)
    assert "N=" in capsys.readouterr().out


def test_repo_skill_copies_match_root():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    canonical = (root / "SKILL.md").read_bytes()
    for sub in (".claude/skills/counterexample/SKILL.md", ".agents/skills/counterexample/SKILL.md"):
        copy = root / sub
        if copy.exists():
            assert copy.read_bytes() == canonical, f"{sub} is stale: run `counterexample install-skill`"
