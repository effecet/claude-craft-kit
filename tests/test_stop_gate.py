"""Tests for stop_gate.py's two silent-failure paths.

* The optional backup after wrap-up must actually spawn with the configured
  interpreter. A failed spawn lands in `except Exception: pass` with output at
  DEVNULL, so a broken path would exit clean while backing up nothing.
* The wrap-up reminder must fire at most once. When recording "we already
  asked" was the model's job, it re-fired on the very next stop.

So these tests are written to assert the HARM, not the mechanism: "the backup
did not spawn", "the reminder fired twice", "the goodbye reminder went silent"
— never `pytest.raises(FileNotFoundError)`. An exception-shaped assertion would
have passed throughout the original outage, because the exception was being
caught on purpose.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

import stop_gate as sg  # noqa: E402


def _state(**over) -> dict:
    base = {
        "session_id": "2026-09-07T00:00:00Z",
        "user_message_count": sg.MIN_MESSAGES_FOR_WRAP_UP + 2,
        "wrap_up_ran": False,
        "wrap_up_prompted": False,
    }
    base.update(over)
    return base


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    """A throwaway state.json, so a regression can never touch the real one."""
    p = tmp_path / "state.json"
    monkeypatch.setattr(sg, "STATE_FILE", p)
    return p


def _run() -> str:
    """Invoke the hook, returning whatever it wrote to stdout."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.suppress(SystemExit):
        sg.main()
    return buf.getvalue()


# ── the optional backup step ────────────────────────────────────────────────


class TestBackupSpawns:
    """The backup must actually RUN after wrap-up, with the configured interpreter."""

    @pytest.fixture
    def spawned(self, monkeypatch, tmp_path):
        calls: list[list[str]] = []
        monkeypatch.setattr(sg.subprocess, "Popen", lambda argv, *a, **k: calls.append(argv) or object())
        backup = tmp_path / "backup.py"
        backup.write_text("")
        monkeypatch.setattr(sg, "BACKUP_SCRIPT", backup)
        return calls

    def test_backup_runs_with_python_bin(self, state_file, spawned, monkeypatch):
        monkeypatch.setattr(sg, "PYTHON", "/opt/py/bin/python3")
        monkeypatch.setattr(sg.shutil, "which", lambda p: p)
        state_file.write_text(json.dumps(_state(wrap_up_ran=True)))

        _run()

        assert spawned == [["/opt/py/bin/python3", str(sg.BACKUP_SCRIPT)]]

    def test_unexecutable_python_bin_is_reported_not_swallowed(self, state_file, spawned, monkeypatch, capsys):
        monkeypatch.setattr(sg, "PYTHON", "/nonexistent/python3")
        state_file.write_text(json.dumps(_state(wrap_up_ran=True)))

        _run()

        assert spawned == []
        assert "backup skipped" in capsys.readouterr().err

    def test_no_backup_before_wrap_up_ran(self, state_file, spawned, monkeypatch):
        monkeypatch.setattr(sg.shutil, "which", lambda p: p)
        # Quiet path (prompt already shown), but wrap-up never ran.
        state_file.write_text(json.dumps(_state(wrap_up_ran=False, stop_gate_prompted=True)))

        _run()

        assert spawned == []

    def test_missing_backup_script_spawns_nothing(self, state_file, spawned, monkeypatch, tmp_path):
        monkeypatch.setattr(sg.shutil, "which", lambda p: p)
        monkeypatch.setattr(sg, "BACKUP_SCRIPT", tmp_path / "gone.py")
        state_file.write_text(json.dumps(_state(wrap_up_ran=True)))

        _run()

        assert spawned == []

    def test_no_backup_script_configured_spawns_nothing(self, state_file, spawned, monkeypatch):
        monkeypatch.setattr(sg, "BACKUP_SCRIPT", None)
        state_file.write_text(json.dumps(_state(wrap_up_ran=True)))

        _run()

        assert spawned == []


# ── the reminder that fired twice ───────────────────────────────────────────


class TestPromptFiresAtMostOnce:
    def test_second_stop_is_silent(self, state_file):
        """Two stops, no model in between. The second must say nothing."""
        state_file.write_text(json.dumps(_state()))

        first, second = _run(), _run()

        assert '"decision"' in first and "block" in first
        assert second == "", "reminder re-fired — the double-prompt regression"

    def test_claim_survives_in_state(self, state_file):
        state_file.write_text(json.dumps(_state()))
        _run()
        assert json.loads(state_file.read_text())["stop_gate_prompted"] is True

    def test_unrelated_state_keys_survive_the_claim(self, state_file):
        """The hook rewrites the whole dict; track.py writes this file too."""
        state_file.write_text(
            json.dumps(_state(custom="keep me", user_message_count=99))
        )

        _run()

        after = json.loads(state_file.read_text())
        assert after["custom"] == "keep me"
        assert after["user_message_count"] == 99
        assert after["session_id"] == "2026-09-07T00:00:00Z"


class TestDoesNotDisarmTheOtherTriggers:
    """The regression a review gate found when the claim first landed.

    `wrap_up_prompted` is read by track.py:340 (message-threshold prompt) and
    track.py:367 (_step_goodbye, the reminder when the user types "ciao"). It is
    also DAY-scoped, because session_start.py resumes the same state.json for
    every session started on the same UTC day. So a stop_gate that claims that
    key silences both triggers for the rest of the day, across sessions — in a
    system whose entire purpose is capturing learnings at wrap-up.
    """

    def test_stop_gate_leaves_wrap_up_prompted_alone(self, state_file):
        state_file.write_text(json.dumps(_state()))

        _run()

        after = json.loads(state_file.read_text())
        assert after["wrap_up_prompted"] is False, (
            "stop_gate claimed wrap_up_prompted; track.py's threshold prompt and "
            "goodbye reminder are now disarmed for the rest of the UTC day"
        )

    def test_model_written_flag_still_suppresses_stop_gate(self, state_file):
        """Splitting the keys must not cost us the original suppression."""
        state_file.write_text(json.dumps(_state(wrap_up_prompted=True)))
        assert _run() == ""


# ── the gates that let a stop through ───────────────────────────────────────


class TestQuietPaths:
    def test_trivial_session_is_not_interrupted(self, state_file):
        state_file.write_text(json.dumps(_state(user_message_count=1)))
        assert _run() == ""

    def test_no_state_file_lets_the_stop_through(self, state_file):
        assert not state_file.exists()
        assert _run() == ""

    def test_corrupt_state_lets_the_stop_through(self, state_file):
        """Fail open: a broken state file must never wedge the session."""
        state_file.write_text("{not json")
        assert _run() == ""
