"""End-to-end wiring of session_start.main(), and settings parsed from the env.

The per-check test files call each check directly, so a check that main() no
longer calls — or whose output never reaches stdout — would pass all of them.
These tests run main() itself.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

import session_start as ss  # noqa: E402
import stop_gate as sg  # noqa: E402

CHECKS = {
    "brain_sync": "SENTINEL-sync",
    "memory_decay": "SENTINEL-decay",
    "evolve_check": "SENTINEL-evolve",
    "config_sync_check": "SENTINEL-config-sync",
    "live_hooks_branch": "SENTINEL-live-hooks",
    "config_untracked": "SENTINEL-untracked",
    "secret_scan_status": "SENTINEL-secret-scan",
    "canary_liveness": "SENTINEL-canary",
    "dedup_warning": "SENTINEL-dedup",
    "brain_check": "SENTINEL-brain",
    "hook_drift": "SENTINEL-hook-drift",
}


def test_every_check_reaches_stdout(tmp_path: Path, monkeypatch, capsys) -> None:
    for name, sentinel in CHECKS.items():
        monkeypatch.setattr(ss, name, lambda *a, _s=sentinel, **k: [_s])
    monkeypatch.setattr(ss, "init_state", lambda: ({}, False))
    monkeypatch.setattr(ss, "write_state", lambda state: None)
    monkeypatch.setattr(ss, "build_brief", lambda state, resumed: "brief")
    monkeypatch.setattr(ss, "BRIEF_FILE", tmp_path / "brief.md")
    monkeypatch.setattr(ss, "STDOUT_OVERFLOW_FILE", tmp_path / "overflow.txt")

    ss.main()

    out = capsys.readouterr().out
    missing = [s for s in CHECKS.values() if s not in out]
    assert missing == [], f"checks not wired into main(): {missing}"
    assert out.rstrip().splitlines()[-1].startswith("stdout: ")


# ── settings parsed at import ─────────────────────────────────────────────────


@pytest.fixture
def reload_with(monkeypatch):
    """Reload a hook module under a given environment; restore it afterwards."""
    reloaded = []

    def _reload(module, **env):
        for key in [k for k in list(__import__("os").environ) if k.startswith("CRAFT_")]:
            monkeypatch.delenv(key, raising=False)
        for key in ("PYTHON_BIN", "BACKUP_SCRIPT"):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        reloaded.append(module)
        return importlib.reload(module)

    yield _reload
    monkeypatch.undo()
    for module in reloaded:
        importlib.reload(module)


def test_unset_settings_mean_off(reload_with) -> None:
    mod = reload_with(ss)
    assert mod.CONFIG_REPO is None
    assert mod.CANARY_FILE is None
    assert mod.CONFIG_SUBDIR == ""
    assert mod.CONFIG_BRANCH == "main"
    assert mod.CANARY_MAX_AGE_H == 3.0
    assert mod.HOOK_DRIFT_EXCLUDE == frozenset({"settings.json"})


def test_settings_are_read_from_the_environment(reload_with, tmp_path: Path) -> None:
    mod = reload_with(
        ss,
        CRAFT_CONFIG_REPO=str(tmp_path / "cfg"),
        CRAFT_CONFIG_SUBDIR="/claude/",
        CRAFT_CONFIG_BRANCH="stable",
        CRAFT_CANARY_FILE=str(tmp_path / "beat"),
        CRAFT_CANARY_MAX_AGE_H="12",
        CRAFT_HOOK_DRIFT_EXCLUDE=" a.sh, b.py ,",
    )
    assert mod.CONFIG_REPO == tmp_path / "cfg"
    assert mod.CONFIG_SUBDIR == "claude"
    assert mod.CONFIG_BRANCH == "stable"
    assert mod.CANARY_FILE == tmp_path / "beat"
    assert mod.CANARY_MAX_AGE_H == 12.0
    assert mod.HOOK_DRIFT_EXCLUDE == frozenset({"settings.json", "a.sh", "b.py"})


def test_unparseable_canary_age_falls_back(reload_with) -> None:
    assert reload_with(ss, CRAFT_CANARY_MAX_AGE_H="soon").CANARY_MAX_AGE_H == 3.0


def test_stop_gate_settings_are_read_from_the_environment(reload_with, tmp_path: Path) -> None:
    mod = reload_with(sg, PYTHON_BIN="/opt/py/bin/python3", BACKUP_SCRIPT=str(tmp_path / "b.py"))
    assert mod.PYTHON == "/opt/py/bin/python3"
    assert mod.BACKUP_SCRIPT == tmp_path / "b.py"
    mod = reload_with(sg)
    assert mod.PYTHON == "python3"
    assert mod.BACKUP_SCRIPT is None


# ── edge cases ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", ["1799999000.5", "1799999000"])
def test_canary_accepts_decimal_and_integer_stamps(tmp_path: Path, text: str) -> None:
    stamp = tmp_path / "beat"
    stamp.write_text(text)
    assert ss.canary_liveness(stamp, 3, 1_800_000_000) == []


@pytest.mark.parametrize("text", ["9" * 400, "nan", "inf", "-5"])
def test_canary_rejects_implausible_stamps_without_raising(tmp_path: Path, text: str) -> None:
    stamp = tmp_path / "beat"
    stamp.write_text(text)
    assert "no run recorded" in ss.canary_liveness(stamp, 3, 1_800_000_000)[0]


def test_bare_tilde_git_hooks_dir_is_the_home_itself(tmp_path: Path) -> None:
    # CRAFT_GIT_HOOKS_DIR="~" names the home dir; it does not exist as a hooks
    # dir with a missing global config, so the check must say the hook is not
    # installed — proving "~" was expanded against `home`, not left literal.
    env = {
        "PATH": "/usr/bin:/bin",
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GITLEAKS_HOOK_SEARCH_PATH": "",
        "CRAFT_GIT_HOOKS_DIR": "~",
    }
    msgs = ss.secret_scan_status(home=tmp_path, env=env)
    assert any("core.hooksPath" in m and str(tmp_path) in m for m in msgs)


def test_relative_path_entries_are_not_searched_for_gitleaks(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".claude" / "git-hooks").mkdir(parents=True)
    planted = tmp_path / "proj"
    planted.mkdir()
    (planted / "gitleaks").write_text("#!/bin/sh\necho 8.30.1\n")
    (planted / "gitleaks").chmod(0o755)
    monkeypatch.chdir(planted)
    env = {
        "PATH": ".:/usr/bin:/bin",
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GITLEAKS_HOOK_SEARCH_PATH": "",
        "CRAFT_GIT_HOOKS_DIR": "~/.claude/git-hooks",
    }
    msgs = ss.secret_scan_status(home=tmp_path, env=env)
    assert any("no gitleaks binary" in m for m in msgs)
