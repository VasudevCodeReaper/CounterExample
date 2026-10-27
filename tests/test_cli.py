import json

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


def test_install_skill_and_generators(tmp_path, capsys):
    assert main(["install-skill", "--dir", str(tmp_path)]) == 0
    for sub in (".claude/skills/counterexample/SKILL.md", ".agents/skills/counterexample/SKILL.md"):
        text = (tmp_path / sub).read_text(encoding="utf-8")
        assert text.startswith("---\nname: counterexample")
    assert main(["generators"]) == 0
    assert "generator_spec reference" in capsys.readouterr().out


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
