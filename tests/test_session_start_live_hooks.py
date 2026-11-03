"""Tests for session_start.live_hooks_branch.

When `~/.claude/hooks` is a symlink into a working clone, the live guards are
whatever branch some session last checked out there, so a merged fix can stay
dead. The check must say so, and stay silent otherwise.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

import session_start as ss  # noqa: E402


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def clone(tmp_path: Path, monkeypatch) -> Path:
    """A kit-style checkout: hooks/ at the repo root (CRAFT_CONFIG_SUBDIR unset)."""
    monkeypatch.setattr(ss, "CONFIG_SUBDIR", "")
    monkeypatch.setattr(ss, "CONFIG_BRANCH", "main")
    repo = tmp_path / "my-config"
    (repo / "hooks").mkdir(parents=True)
    (repo / "hooks" / "x.py").write_text("x\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(
        repo,
        "-c", "user.email=t@x",
        "-c", "user.name=t",
        "-c", "commit.gpgsign=false",
        "commit", "-qm", "init",
    )
    monkeypatch.setattr(ss, "CONFIG_REPO", repo)
    return repo


@pytest.fixture
def link(tmp_path: Path, clone: Path) -> Path:
    hooks = tmp_path / "dot-claude" / "hooks"
    hooks.parent.mkdir()
    hooks.symlink_to(clone / "hooks")
    return hooks


def test_silent_when_config_repo_is_not_set(link: Path, clone: Path, monkeypatch) -> None:
    _git(clone, "switch", "-qc", "feat/x")
    monkeypatch.setattr(ss, "CONFIG_REPO", None)
    assert ss.live_hooks_branch(link) == []


def test_silent_for_a_checkout_other_than_the_configured_one(link: Path, clone: Path, monkeypatch, tmp_path: Path) -> None:
    _git(clone, "switch", "-qc", "feat/x")
    monkeypatch.setattr(ss, "CONFIG_REPO", tmp_path / "another-clone")
    assert ss.live_hooks_branch(link) == []


def test_silent_on_main(link: Path) -> None:
    assert ss.live_hooks_branch(link) == []


def test_feature_branch_is_named(link: Path, clone: Path) -> None:
    _git(clone, "switch", "-qc", "feat/x")
    msgs = ss.live_hooks_branch(link)
    assert len(msgs) == 1 and "feat/x" in msgs[0] and "not main" in msgs[0]
    assert str(clone.resolve()) in msgs[0]  # the real repo root, from git


def test_configured_branch_replaces_main(link: Path, clone: Path, monkeypatch) -> None:
    _git(clone, "switch", "-qc", "stable")
    monkeypatch.setattr(ss, "CONFIG_BRANCH", "stable")
    assert ss.live_hooks_branch(link) == []
    _git(clone, "switch", "-q", "main")
    assert "not stable" in ss.live_hooks_branch(link)[0]


def test_detached_head_is_flagged(link: Path, clone: Path) -> None:
    _git(clone, "checkout", "-q", "--detach")
    msgs = ss.live_hooks_branch(link)
    assert len(msgs) == 1 and "detached" in msgs[0]


def test_real_directory_is_not_checked(clone: Path) -> None:
    # Hooks deployed as real files say nothing: only a symlink is checked.
    _git(clone, "switch", "-qc", "feat/x")
    assert ss.live_hooks_branch(clone / "hooks") == []


def test_symlink_loop_fails_open(tmp_path: Path) -> None:
    # A loop raises RuntimeError in resolve(); uncaught, it would end main()
    # and drop every later section of the session brief.
    (tmp_path / "a").symlink_to(tmp_path / "b")
    (tmp_path / "b").symlink_to(tmp_path / "a")
    assert ss.live_hooks_branch(tmp_path / "a") == []


def test_subdir_layout_needs_the_setting(tmp_path: Path, monkeypatch) -> None:
    # A checkout that keeps hooks under a subdirectory is only recognised when
    # CRAFT_CONFIG_SUBDIR names it; otherwise the link is treated as unrelated.
    monkeypatch.setattr(ss, "CONFIG_BRANCH", "main")
    repo = tmp_path / "dotfiles"
    (repo / "claude" / "hooks").mkdir(parents=True)
    (repo / "claude" / "hooks" / "x.py").write_text("x\n")
    _git(repo, "init", "-q", "-b", "feat")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.email=t@x", "-c", "user.name=t", "-c", "commit.gpgsign=false", "commit", "-qm", "init")
    hooks = tmp_path / "dot-claude" / "hooks"
    hooks.parent.mkdir()
    hooks.symlink_to(repo / "claude" / "hooks")
    monkeypatch.setattr(ss, "CONFIG_REPO", repo)

    monkeypatch.setattr(ss, "CONFIG_SUBDIR", "")
    assert ss.live_hooks_branch(hooks) == []
    monkeypatch.setattr(ss, "CONFIG_SUBDIR", "claude")
    assert "feat" in ss.live_hooks_branch(hooks)[0]
