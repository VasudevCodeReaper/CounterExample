"""Project-local setup for coding agents that can use Counterexample.

The engine is model-neutral: an MCP-capable host can expose its tools to the
model selected in that host. This module installs host-specific project files
without replacing a project's existing policy or MCP servers.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from counterexample.resources import skill_path

MANAGED_BEGIN = "<!-- counterexample:begin -->"
MANAGED_END = "<!-- counterexample:end -->"

MCP_ENTRY = {
    "command": "python",
    "args": ["-m", "counterexample.mcp_server"],
    "env": {"SANDBOX_TIMEOUT_MS": "2000", "DEFAULT_ARENA_RUNS": "5000"},
}
VSCODE_MCP_ENTRY = {"type": "stdio", **MCP_ENTRY}

_INSTRUCTIONS = """## Counterexample verification

When changing behaviour, fixing a bug, refactoring non-trivial logic, or reviewing generated
code, use the installed Counterexample workflow before calling the work verified. Read the
project's Counterexample skill when available, use an independent oracle or a clearly stated
property, preserve generated counterexamples as regression tests, and report the evidence and
remaining limits. Do not weaken an oracle, property, or input generator to make a failure vanish.
"""

_CURSOR_RULE = """---
description: Use Counterexample's adversarial falsification workflow for behaviour-changing code, bug fixes, non-trivial refactors, and generated-code review.
alwaysApply: false
---

"""

_COPILOT_AGENT = """---
name: Counterexample
description: Falsify behavior-changing code with independent oracles, adversarial inputs, shrinking, and complexity checks before declaring it verified.
---

"""


def _managed_block(body: str = _INSTRUCTIONS) -> str:
    return f"{MANAGED_BEGIN}\n{body.rstrip()}\n{MANAGED_END}\n"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _upsert_instruction(path: Path, body: str = _INSTRUCTIONS) -> str:
    """Append or refresh our tagged block while preserving all other policy."""

    text = _read(path)
    block = _managed_block(body)
    start = text.find(MANAGED_BEGIN)
    end = text.find(MANAGED_END)
    if start >= 0 or end >= 0:
        if start < 0 or end < start:
            return f"skipped malformed managed block in {path}"
        updated = text[:start] + block + text[end + len(MANAGED_END) :].lstrip("\r\n")
        if updated != text:
            _write(path, updated)
            return f"updated {path}"
        return f"already configured {path}"
    separator = "" if not text or text.endswith(("\n", "\r")) else "\n"
    _write(path, text + separator + ("\n" if text else "") + block)
    return f"updated {path}"


def _write_owned_rule(path: Path, prefix: str) -> str:
    """Write an agent-specific rule only when it is ours or does not exist."""

    text = _read(path)
    if not text:
        _write(path, prefix + _managed_block())
        return f"installed {path}"
    if MANAGED_BEGIN in text and MANAGED_END in text:
        return _upsert_instruction(path)
    return f"skipped existing unmanaged file {path}"


def _install_skill(root: Path, targets: list[str]) -> list[str]:
    src = skill_path()
    actions = []
    for target in targets:
        dest = root / target / "SKILL.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        actions.append(f"installed {dest}")
    return actions


def _merge_mcp_json(path: Path, container: str, entry: dict[str, Any]) -> str:
    """Add Counterexample without replacing another agent's JSON settings."""

    raw = _read(path)
    if raw.strip():
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            return f"skipped invalid JSON in {path}: {exc.msg}"
        if not isinstance(data, dict):
            return f"skipped non-object JSON in {path}"
    else:
        data = {}
    servers = data.setdefault(container, {})
    if not isinstance(servers, dict):
        return f"skipped non-object {container} in {path}"
    existing = servers.get("counterexample")
    if existing is None:
        servers["counterexample"] = entry
        _write(path, json.dumps(data, indent=2) + "\n")
        return f"configured MCP server in {path}"
    if existing == entry:
        return f"already configured MCP server in {path}"
    return f"skipped existing counterexample MCP server in {path}"


def _upsert_codex_config(root: Path) -> str:
    path = root / ".codex" / "config.toml"
    text = _read(path)
    begin = "# counterexample:begin"
    end_marker = "# counterexample:end"
    block = (
        f"{begin}\n"
        "[mcp_servers.counterexample]\n"
        'command = "python"\n'
        'args = ["-m", "counterexample.mcp_server"]\n\n'
        "[mcp_servers.counterexample.env]\n"
        'SANDBOX_TIMEOUT_MS = "2000"\n'
        'DEFAULT_ARENA_RUNS = "5000"\n'
        f"{end_marker}\n"
    )
    if begin in text and end_marker in text:
        start, end = text.find(begin), text.find(end_marker)
        _write(path, text[:start] + block + text[end + len(end_marker) :].lstrip("\r\n"))
        return f"configured MCP server in {path}"
    if re.search(r"^\[mcp_servers\.counterexample\]$", text, flags=re.MULTILINE):
        return f"skipped existing counterexample MCP server in {path}"
    separator = "" if not text or text.endswith(("\n", "\r")) else "\n"
    _write(path, text + separator + ("\n" if text else "") + block)
    return f"configured MCP server in {path}"


def _windsurf_config_path() -> Path:
    return Path.home() / ".codeium" / "windsurf" / "mcp_config.json"


def _cline_config_path() -> Path:
    """Cline CLI's user-level MCP configuration path."""

    return Path.home() / ".cline" / "data" / "settings" / "cline_mcp_settings.json"


def initialize_project(
    root: Path,
    *,
    target: str = "all",
    configure_windsurf: bool = False,
    configure_cline: bool = False,
) -> list[str]:
    """Install project-scoped compatibility assets and return actions taken.

    ``all`` remains project-local. Windsurf has only a user-level MCP config,
    so updating it needs the separate explicit flag.
    """

    if not root.is_dir():
        raise ValueError(f"project directory does not exist: {root}")
    valid = {"all", "codex", "claude", "cursor", "gemini", "copilot", "windsurf", "cline", "continue"}
    if target not in valid:
        raise ValueError(f"unknown target: {target}")

    use = lambda name: target in ("all", name)
    actions: list[str] = []
    if use("codex"):
        actions += _install_skill(root, [".agents/skills/counterexample"])
        actions.append(_upsert_instruction(root / "AGENTS.md"))
        actions.append(_upsert_codex_config(root))
    if use("claude"):
        actions += _install_skill(root, [".claude/skills/counterexample"])
        actions.append(_upsert_instruction(root / "CLAUDE.md"))
        actions.append(_merge_mcp_json(root / ".mcp.json", "mcpServers", MCP_ENTRY))
    if use("cursor"):
        actions.append(_upsert_instruction(root / "AGENTS.md"))
        actions.append(_write_owned_rule(root / ".cursor" / "rules" / "counterexample.mdc", _CURSOR_RULE))
        actions.append(_merge_mcp_json(root / ".cursor" / "mcp.json", "mcpServers", MCP_ENTRY))
    if use("gemini"):
        actions.append(_upsert_instruction(root / "GEMINI.md"))
        actions.append(_merge_mcp_json(root / ".gemini" / "settings.json", "mcpServers", MCP_ENTRY))
    if use("copilot"):
        actions += _install_skill(root, [".github/skills/counterexample"])
        actions.append(_upsert_instruction(root / "AGENTS.md"))
        actions.append(_upsert_instruction(root / ".github" / "copilot-instructions.md"))
        actions.append(_write_owned_rule(root / ".github" / "agents" / "counterexample.agent.md", _COPILOT_AGENT))
        actions.append(_merge_mcp_json(root / ".vscode" / "mcp.json", "servers", VSCODE_MCP_ENTRY))
    if use("cline"):
        actions += _install_skill(root, [".cline/skills/counterexample"])
        actions.append(_write_owned_rule(root / ".cline" / "rules" / "counterexample.md", ""))
    if use("continue"):
        # Continue auto-discovers JSON MCP configs placed in this workspace directory.
        actions.append(_merge_mcp_json(root / ".continue" / "mcpServers" / "counterexample.json", "mcpServers", MCP_ENTRY))
    if target == "windsurf" and not configure_windsurf:
        actions.append("Windsurf uses a user-level MCP config; rerun with --configure-windsurf to add it")
    if target == "cline" and not configure_cline:
        actions.append("Cline CLI uses a user-level MCP config; rerun with --configure-cline to add it")
    if configure_windsurf:
        actions.append(_merge_mcp_json(_windsurf_config_path(), "mcpServers", MCP_ENTRY))
    elif target == "all":
        actions.append("Windsurf is unchanged: rerun with --configure-windsurf to opt into its user-level MCP config")
    if configure_cline:
        actions.append(_merge_mcp_json(_cline_config_path(), "mcpServers", MCP_ENTRY))
    elif target == "all":
        actions.append("Cline CLI is unchanged: rerun with --configure-cline to opt into its user-level MCP config")
    return actions


__all__ = ["initialize_project"]
