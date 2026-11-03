"""Tests for session_start.secret_scan_status, the brief's secret-scan check.

A global git pre-commit hook that runs gitleaks typically fails OPEN when
gitleaks is missing — one stderr line per commit, easy to miss — and CLOSED
on a gitleaks too old for `gitleaks git`. Either way a machine can sit
unscanned or stuck for weeks. The session brief says so once per session;
silent when fine, and silent unless CRAFT_GIT_HOOKS_DIR is set.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

import session_start as ss  # noqa: E402


@pytest.fixture
def home(tmp_path: Path) -> Path:
    (tmp_path / ".claude" / "git-hooks").mkdir(parents=True)
    return tmp_path


def _env(home: Path, path: str = "/usr/bin:/bin") -> dict[str, str]:
    return {
        **os.environ,
        "HOME": str(home),
        "PATH": path,
        "GIT_CONFIG_GLOBAL": str(home / "gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GITLEAKS_HOOK_SEARCH_PATH": "",
        "CRAFT_GIT_HOOKS_DIR": "~/.claude/git-hooks",
    }


def _set_hooks_path(env: dict[str, str], value: str) -> None:
    subprocess.run(
        ["git", "config", "--global", "core.hooksPath", value], check=True, env=env
    )


def _fake_gitleaks(home: Path, version: str) -> str:
    bindir = home / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "gitleaks"
    fake.write_text(f"#!/bin/sh\necho {version}\n")
    fake.chmod(0o755)
    return f"{bindir}:/usr/bin:/bin"


def test_silent_when_not_configured(home: Path) -> None:
    env = _env(home)
    del env["CRAFT_GIT_HOOKS_DIR"]
    assert ss.secret_scan_status(home=home, env=env) == []


def test_silent_when_installed_and_gitleaks_is_current(home: Path) -> None:
    env = _env(home, _fake_gitleaks(home, "8.30.1"))
    _set_hooks_path(env, str(home / ".claude" / "git-hooks"))
    assert ss.secret_scan_status(home=home, env=env) == []


def test_warns_when_the_hook_is_not_installed(home: Path) -> None:
    env = _env(home, _fake_gitleaks(home, "8.30.1"))
    msgs = ss.secret_scan_status(home=home, env=env)
    assert len(msgs) == 1
    assert "core.hooksPath" in msgs[0]


def test_warns_when_hooks_path_points_elsewhere(home: Path) -> None:
    env = _env(home, _fake_gitleaks(home, "8.30.1"))
    _set_hooks_path(env, "/somewhere/else")
    msgs = ss.secret_scan_status(home=home, env=env)
    assert len(msgs) == 1
    assert "/somewhere/else" in msgs[0]


def test_tilde_hooks_path_counts_as_installed(home: Path) -> None:
    env = _env(home, _fake_gitleaks(home, "8.30.1"))
    _set_hooks_path(env, "~/.claude/git-hooks")
    assert ss.secret_scan_status(home=home, env=env) == []


def test_warns_when_gitleaks_is_missing(home: Path) -> None:
    env = _env(home)
    _set_hooks_path(env, str(home / ".claude" / "git-hooks"))
    msgs = ss.secret_scan_status(home=home, env=env)
    assert len(msgs) == 1
    assert "gitleaks" in msgs[0] and "NOT scanned" in msgs[0]


def test_fallback_search_path_finds_gitleaks(home: Path) -> None:
    bindir = _fake_gitleaks(home, "8.30.1").split(":")[0]
    env = {**_env(home), "GITLEAKS_HOOK_SEARCH_PATH": bindir}
    _set_hooks_path(env, str(home / ".claude" / "git-hooks"))
    assert ss.secret_scan_status(home=home, env=env) == []


@pytest.mark.parametrize("version", ["8.18.4", "v8.18.0", "7.6.1"])
def test_warns_when_gitleaks_is_too_old(home: Path, version: str) -> None:
    env = _env(home, _fake_gitleaks(home, version))
    _set_hooks_path(env, str(home / ".claude" / "git-hooks"))
    msgs = ss.secret_scan_status(home=home, env=env)
    assert len(msgs) == 1
    assert "8.19" in msgs[0]


@pytest.mark.parametrize("version", ["8.19.0", "v8.30.1", "9.0.0"])
def test_new_enough_versions_are_silent(home: Path, version: str) -> None:
    env = _env(home, _fake_gitleaks(home, version))
    _set_hooks_path(env, str(home / ".claude" / "git-hooks"))
    assert ss.secret_scan_status(home=home, env=env) == []


def test_unparseable_version_is_not_reported_as_old(home: Path) -> None:
    env = _env(home, _fake_gitleaks(home, "dev"))
    _set_hooks_path(env, str(home / ".claude" / "git-hooks"))
    assert ss.secret_scan_status(home=home, env=env) == []


def test_dangling_git_hooks_link_warns(tmp_path: Path) -> None:
    # The clone ~/.claude/git-hooks points into sits on a branch without
    # git-hooks dir: git runs no hooks, silently.
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "git-hooks").symlink_to(tmp_path / "clone" / "gone")
    msgs = ss.secret_scan_status(home=tmp_path, env=_env(tmp_path))
    assert len(msgs) == 1
    assert "NOT scanned" in msgs[0] and "main" in msgs[0]


def test_hook_and_gitleaks_both_missing_report_both(home: Path) -> None:
    msgs = ss.secret_scan_status(home=home, env=_env(home))
    assert len(msgs) == 2
    assert "core.hooksPath" in msgs[0]
    assert "gitleaks" in msgs[1]


def test_tilde_only_expands_as_a_prefix(home: Path) -> None:
    env = _env(home, _fake_gitleaks(home, "8.30.1"))
    _set_hooks_path(env, "/opt/x~/git-hooks")
    msgs = ss.secret_scan_status(home=home, env=env)
    assert len(msgs) == 1
    assert "/opt/x~/git-hooks" in msgs[0]


def test_tilde_in_the_middle_is_not_home(home: Path) -> None:
    # git expands only a LEADING ~; `/~/.claude/git-hooks` is a real, other path.
    env = _env(home, _fake_gitleaks(home, "8.30.1"))
    _set_hooks_path(env, "/~/.claude/git-hooks")
    assert len(ss.secret_scan_status(home=home, env=env)) == 1


def test_machine_without_claude_git_hooks_is_silent(tmp_path: Path) -> None:
    # The configured hooks dir does not exist here: nothing to promise.
    assert ss.secret_scan_status(home=tmp_path, env=_env(tmp_path)) == []


def test_wired_into_the_brief() -> None:
    src = (Path(__file__).resolve().parent.parent / "hooks" / "session_start.py").read_text()
    assert "secret_scan_status()" in src
