"""Locate packaged resources (the portable SKILL.md)."""

from __future__ import annotations

from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent


def skill_path() -> Path:
    """Path to SKILL.md: the copy bundled in the wheel, else the repo root (editable installs)."""
    for candidate in (_PKG_DIR / "_bundled" / "SKILL.md", _PKG_DIR.parent / "SKILL.md"):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("SKILL.md not found in the installed package")


def skill_text() -> str:
    return skill_path().read_text(encoding="utf-8")
