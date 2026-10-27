from __future__ import annotations

import shutil
import subprocess

import pytest


def _has_cxx() -> bool:
    from counterexample.adapters.base import WorkerError
    from counterexample.adapters.cpp_runner import find_compiler

    try:
        find_compiler()
        return True
    except WorkerError:
        return False


def _has_fastapi() -> bool:
    try:
        import fastapi  # noqa: F401
        import httpx  # noqa: F401
    except ImportError:
        return False
    return True


HAS_CXX = _has_cxx()
HAS_FASTAPI = _has_fastapi()
HAS_GIT = shutil.which("git") is not None

requires_cxx = pytest.mark.skipif(not HAS_CXX, reason="no C++ compiler available")
requires_fastapi = pytest.mark.skipif(not HAS_FASTAPI, reason="fastapi/httpx not installed")
requires_git = pytest.mark.skipif(not HAS_GIT, reason="git not installed")


class Repo:
    """A temporary git repository; ``repo / "file"`` gives paths inside it."""

    def __init__(self, path):
        self.path = path

    def __truediv__(self, other):
        return self.path / other

    def __fspath__(self):
        return str(self.path)

    def __str__(self):
        return str(self.path)

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.path, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def git_repo(tmp_path):
    """An empty git repository with a committer identity configured."""
    repo = Repo(tmp_path)
    repo.git("init", "-q")
    repo.git("config", "user.email", "ci@example.com")
    repo.git("config", "user.name", "CI")
    repo.git("config", "commit.gpgsign", "false")
    return repo
