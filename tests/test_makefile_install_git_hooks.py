"""Tests for `make install-git-hooks`, the step that turns the secret scan on.

It copies git-hooks/ into CLAUDE_DIR and points the GLOBAL core.hooksPath at
it, so each test gets its own throwaway global gitconfig (GIT_CONFIG_GLOBAL)
and HOME, and never touches the real one.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="needs make")


def _env(tmp_path: Path, gitconfig: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path / "home"),
        "GIT_CONFIG_GLOBAL": str(gitconfig),
        "GIT_CONFIG_NOSYSTEM": "1",
    }


def _install(tmp_path: Path, gitconfig: Path, claude_dir: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "home").mkdir(exist_ok=True)
    return subprocess.run(
        ["make", "-s", "install-git-hooks", f"CLAUDE_DIR={claude_dir}"],
        cwd=REPO,
        env=_env(tmp_path, gitconfig),
        capture_output=True,
        text=True,
        timeout=60,
    )


def _hooks_path(tmp_path: Path, gitconfig: Path) -> str:
    return subprocess.run(
        ["git", "config", "--global", "--get", "core.hooksPath"],
        env=_env(tmp_path, gitconfig),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_installs_hooks_as_symlinks_and_sets_the_global_path(tmp_path: Path) -> None:
    gc = tmp_path / "gitconfig"
    proc = _install(tmp_path, gc, str(tmp_path / "claude"))
    assert proc.returncode == 0, proc.stderr
    hooks = tmp_path / "claude" / "git-hooks"
    assert _hooks_path(tmp_path, gc) == str(hooks)
    assert os.access(hooks / "dispatch", os.X_OK)
    # Links, not copies: a dereferenced copy would go stale on the next update.
    assert (hooks / "pre-commit").is_symlink()
    assert os.readlink(hooks / "pre-commit") == "dispatch"


def test_refuses_to_overwrite_another_global_hooks_path(tmp_path: Path) -> None:
    gc = tmp_path / "gitconfig"
    gc.write_text("[core]\n\thooksPath = /elsewhere\n")
    proc = _install(tmp_path, gc, str(tmp_path / "claude"))
    assert proc.returncode != 0
    assert "not overwriting" in proc.stderr
    assert _hooks_path(tmp_path, gc) == "/elsewhere"
    assert not (tmp_path / "claude" / "git-hooks").exists()


@pytest.mark.parametrize("written", ["~/.claude/git-hooks", "~/.claude/git-hooks/"])
def test_rerun_accepts_the_same_dir_written_another_way(tmp_path: Path, written: str) -> None:
    gc = tmp_path / "gitconfig"
    gc.write_text(f"[core]\n\thooksPath = {written}\n")
    proc = _install(tmp_path, gc, str(tmp_path / "home" / ".claude"))
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "home" / ".claude" / "git-hooks" / "pre-commit").is_symlink()


def test_relative_claude_dir_is_stored_absolute(tmp_path: Path) -> None:
    # git resolves a relative core.hooksPath per repo, which scans nothing.
    gc = tmp_path / "gitconfig"
    rel = os.path.relpath(tmp_path / "claude", REPO)
    proc = _install(tmp_path, gc, rel)
    assert proc.returncode == 0, proc.stderr
    stored = _hooks_path(tmp_path, gc)
    assert Path(stored).is_absolute()
    assert Path(stored).resolve() == (tmp_path / "claude" / "git-hooks").resolve()
