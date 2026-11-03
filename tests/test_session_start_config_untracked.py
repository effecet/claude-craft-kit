"""Tests for session_start.config_untracked, the brief's uncommitted-config check.

A new skill, rule or hook dropped into the config checkout is live at once
but not in git, so it can sit untracked for weeks with nothing noticing — and
is lost with the machine.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

import session_start as ss  # noqa: E402


@pytest.fixture(autouse=True)
def _upstream_style_layout(monkeypatch):
    """These fixtures build `<repo>/claude-config/{hooks,scripts}`, so point the
    CRAFT_CONFIG_SUBDIR setting at it (the kit's own default is the root)."""
    monkeypatch.setattr(ss, "CONFIG_SUBDIR", "claude-config")


def _git(repo: Path, *args: str) -> None:
    # No signing and no global hooks: the machine's gitleaks dispatcher must not
    # run on fixture commits.
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
         "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def brain(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """A config checkout with one committed hook, and ~/.claude/hooks linked to it."""
    repo = tmp_path / "my-config"
    hooks = repo / "claude-config" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "track.py").write_text("x = 1\n")
    # A tracked sibling, as in the real repo: git reports an untracked dir at
    # its topmost untracked ancestor, so without it the skill shows as skills/.
    old = repo / "claude-config" / "skills" / "learned" / "old-skill"
    old.mkdir(parents=True)
    (old / "SKILL.md").write_text("---\nname: old-skill\n---\n")
    (repo / ".gitignore").write_text("*.log\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    link = tmp_path / "home" / ".claude" / "hooks"
    link.parent.mkdir(parents=True)
    link.symlink_to(hooks)
    monkeypatch.setattr(ss, "CONFIG_REPO", repo)
    return repo, link


def test_silent_when_config_repo_is_not_set(brain, monkeypatch) -> None:
    repo, link = brain
    (repo / "claude-config" / "new.md").write_text("x\n")
    monkeypatch.setattr(ss, "CONFIG_REPO", None)
    assert ss.config_untracked(link) == []


def test_silent_for_a_checkout_other_than_the_configured_one(brain, monkeypatch, tmp_path) -> None:
    repo, link = brain
    (repo / "claude-config" / "new.md").write_text("x\n")
    monkeypatch.setattr(ss, "CONFIG_REPO", tmp_path / "somewhere-else")
    assert ss.config_untracked(link) == []


def test_clean_checkout_is_silent(brain):
    _, link = brain
    assert ss.config_untracked(link) == []


def test_untracked_learned_skill_is_named(brain):
    repo, link = brain
    skill = repo / "claude-config" / "skills" / "learned" / "new-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: new-skill\n---\n")
    msgs = ss.config_untracked(link)
    assert len(msgs) == 1
    assert "1 untracked" in msgs[0]
    assert "claude-config/skills/learned/new-skill/" in msgs[0]
    assert str(repo) in msgs[0]


def test_modified_tracked_file_is_not_reported(brain):
    # Routine churn (generated logs) is modified, not untracked: not this check's job.
    repo, link = brain
    (repo / "claude-config" / "hooks" / "track.py").write_text("x = 2\n")
    assert ss.config_untracked(link) == []


def test_untracked_file_outside_claude_config_is_not_reported(brain):
    repo, link = brain
    (repo / "scratch.txt").write_text("notes\n")
    assert ss.config_untracked(link) == []


def test_ignored_file_is_not_reported(brain):
    repo, link = brain
    (repo / "claude-config" / "run.log").write_text("noise\n")
    assert ss.config_untracked(link) == []


def test_long_list_is_truncated_with_a_count(brain):
    repo, link = brain
    for i in range(5):
        (repo / "claude-config" / f"new{i}.md").write_text("x\n")
    msg = ss.config_untracked(link)[0]
    assert "5 untracked" in msg
    assert "(+2 more)" in msg


def test_unicode_name_is_shown_raw_not_git_quoted(brain):
    # Without -z git would print "claude-config/\303\261 \303\251.md" in quotes.
    repo, link = brain
    (repo / "claude-config" / "ñ é.md").write_text("x\n")
    assert "claude-config/ñ é.md" in ss.config_untracked(link)[0]


def test_control_characters_in_a_name_are_masked(brain):
    repo, link = brain
    (repo / "claude-config" / "evil\x1b[2Jname.md").write_text("x\n")
    msg = ss.config_untracked(link)[0]
    assert "\x1b" not in msg
    assert "evil?[2Jname.md" in msg


def test_a_hung_git_status_is_silent(brain, monkeypatch):
    _, link = brain
    real_run = subprocess.run

    def fake_run(argv, **kw):
        if "status" in argv:
            raise subprocess.TimeoutExpired(argv, 5)
        return real_run(argv, **kw)

    monkeypatch.setattr(ss.subprocess, "run", fake_run)
    assert ss.config_untracked(link) == []


def test_real_hooks_dir_is_silent(tmp_path):
    # Hooks deployed as real-file copies: there is no checkout to inspect.
    hooks = tmp_path / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    assert ss.config_untracked(hooks) == []


def test_link_outside_a_git_checkout_is_silent(tmp_path):
    hooks = tmp_path / "claude-config" / "hooks"
    hooks.mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(hooks)
    assert ss.config_untracked(link) == []
