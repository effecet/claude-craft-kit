"""Tests for hooks/token_tracker.py and how track.py finds it."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import token_tracker as tt
import track

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
PRICE_VARS = (
    "CRAFT_PRICE_INPUT_PER_M",
    "CRAFT_PRICE_OUTPUT_PER_M",
    "CRAFT_PRICE_CACHE_WRITE_PER_M",
    "CRAFT_PRICE_CACHE_READ_PER_M",
    "CRAFT_CONTEXT_WINDOW",
)


@pytest.fixture
def reload_tt(monkeypatch: pytest.MonkeyPatch):
    """Reload token_tracker under a patched environment, restoring it after."""
    for name in PRICE_VARS:
        monkeypatch.delenv(name, raising=False)

    def _reload(**env: str):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return importlib.reload(tt)

    yield _reload
    monkeypatch.undo()
    importlib.reload(tt)


def test_track_runs_the_sibling_it_ships_with() -> None:
    # It used to look in CLAUDE_DIR, where `make install` never puts it.
    assert track.TOKEN_TRACKER == HOOKS / "token_tracker.py"
    assert track.TOKEN_TRACKER.is_file()


def test_defaults_without_env(reload_tt) -> None:
    mod = reload_tt()
    assert (mod.PRICE_INPUT_PER_M, mod.PRICE_OUTPUT_PER_M) == (15.00, 75.00)
    assert (mod.PRICE_CACHE_WRITE_PER_M, mod.PRICE_CACHE_READ_PER_M) == (18.75, 1.50)
    assert mod.CONTEXT_WINDOW == 1_000_000


def test_env_overrides_prices_and_window(reload_tt) -> None:
    mod = reload_tt(
        CRAFT_PRICE_INPUT_PER_M="3",
        CRAFT_PRICE_OUTPUT_PER_M="15",
        CRAFT_PRICE_CACHE_WRITE_PER_M="3.75",
        CRAFT_PRICE_CACHE_READ_PER_M="0.3",
        CRAFT_CONTEXT_WINDOW="200000",
    )
    assert mod.CONTEXT_WINDOW == 200_000
    totals = {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cache_creation_input_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
    }
    assert mod.calculate_cost(totals) == pytest.approx(3 + 15 + 3.75 + 0.3)


@pytest.mark.parametrize("bad", ["", "abc", "0", "-5", "nan", "inf", "1e400"])
def test_invalid_value_falls_back_to_default(reload_tt, bad: str) -> None:
    mod = reload_tt(CRAFT_PRICE_INPUT_PER_M=bad)
    assert mod.PRICE_INPUT_PER_M == 15.00


@pytest.mark.parametrize("bad", ["0.5", "1", "inf", "1e400", "nan", "-1"])
def test_invalid_context_window_falls_back(reload_tt, bad: str) -> None:
    # 0.5 would be 0 after int() and divide by zero; inf would not convert.
    mod = reload_tt(CRAFT_CONTEXT_WINDOW=bad)
    assert mod.CONTEXT_WINDOW == 1_000_000
    assert mod.context_estimate({"input_tokens": 1, "output_tokens": 1})[1] >= 0


def test_cache_saving_is_net_of_the_cache_read_price(reload_tt) -> None:
    mod = reload_tt()
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 1_000_000,
    }
    assert "(saves $13.5000)" in mod.format_summary(totals, 1)


def test_end_to_end_writes_state_under_claude_dir(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        json.dumps({"message": {"usage": {"input_tokens": 1000, "output_tokens": 500}}})
        + "\n"
    )
    claude_dir = tmp_path / "claude"
    env = {k: v for k, v in os.environ.items() if k not in PRICE_VARS}
    env.update(CLAUDE_DIR=str(claude_dir), HOME=str(tmp_path / "home"))
    proc = subprocess.run(
        [sys.executable, str(HOOKS / "token_tracker.py"), str(transcript)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Token Status" in proc.stdout
    assert "(estimate)" in proc.stdout
    state = json.loads((claude_dir / "session/state.json").read_text())
    assert state["tokens"]["input_tokens"] == 1000
    assert state["tokens"]["output_tokens"] == 500
    assert not (tmp_path / "home").exists()


def test_track_keeps_the_tokens_token_tracker_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # token_tracker writes "tokens" to the same file track.py saves, so it runs
    # after that save: track's copy must not wipe the fresh tokens, and the
    # tracker must see this turn's count ("Messages" was one behind before).
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"user_message_count": track.TOKEN_REPORT_INTERVAL - 1}))
    monkeypatch.setattr(track, "STATE_FILE", state_file)
    monkeypatch.setattr(track, "_parse_hook_input", lambda: {"transcript_path": "t.jsonl"})
    monkeypatch.setattr(track, "_resolve_transcript", lambda hook, state: "t.jsonl")
    for step in ("_step_backup", "_step_staleness", "_step_threshold_prompt",
                 "_step_friction", "_step_goodbye", "_step_breadcrumb"):
        monkeypatch.setattr(track, step, lambda *a, **k: None)

    seen: list[int] = []

    def fake_report(path: str) -> None:
        on_disk = json.loads(state_file.read_text())
        seen.append(on_disk["user_message_count"])
        on_disk["tokens"] = {"input_tokens": 42}
        state_file.write_text(json.dumps(on_disk))

    monkeypatch.setattr(track, "run_token_report", fake_report)
    monkeypatch.setattr(track, "_desktop_notify", lambda *a: None)
    track.main()
    saved = json.loads(state_file.read_text())
    assert saved["tokens"] == {"input_tokens": 42}
    assert saved["user_message_count"] == track.TOKEN_REPORT_INTERVAL
    assert seen == [track.TOKEN_REPORT_INTERVAL]


def _isolate_track(monkeypatch: pytest.MonkeyPatch, state_file: Path) -> None:
    monkeypatch.setattr(track, "STATE_FILE", state_file)
    monkeypatch.setattr(track, "_parse_hook_input", lambda: {})
    monkeypatch.setattr(track, "_resolve_transcript", lambda hook, state: "t.jsonl")
    for step in ("_step_backup", "_step_staleness", "_step_threshold_prompt",
                 "_step_friction", "_step_goodbye", "_step_breadcrumb"):
        monkeypatch.setattr(track, step, lambda *a, **k: None)
    monkeypatch.setattr(track, "_desktop_notify", lambda *a: None)


def test_day_reset_does_not_bring_back_yesterdays_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({
        "session_id": "2000-01-01T00:00:00Z",
        "user_message_count": 41,
        "tokens": {"input_tokens": 999},
    }))
    _isolate_track(monkeypatch, state_file)
    monkeypatch.setattr(track, "run_token_report", lambda path: None)
    track.main()
    saved = json.loads(state_file.read_text())
    assert saved["user_message_count"] == 1
    assert "tokens" not in saved


def test_token_report_runs_under_this_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(track.subprocess, "run", lambda argv, **kw: calls.append(argv))
    track.run_token_report("t.jsonl")
    assert calls and calls[0][0] == sys.executable


def test_track_and_token_tracker_share_one_state_file() -> None:
    # token_tracker updates the state track.main just saved: one file for both.
    assert track.STATE_FILE == tt.STATE_FILE


def test_missing_tracker_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(track, "TOKEN_TRACKER", tmp_path / "token_tracker.py")
    monkeypatch.setattr(track.subprocess, "run", lambda *a, **k: pytest.fail("ran a missing tracker"))
    track.run_token_report("t.jsonl")
    assert "token_tracker.py not found beside track.py" in capsys.readouterr().err


def test_backup_script_is_where_make_install_puts_it() -> None:
    # `make install` copies hooks/optional/ next to track.py.
    assert track.BACKUP_SCRIPT == HOOKS / "optional" / "backup_hook.py"
    assert track.BACKUP_SCRIPT.exists()


@pytest.mark.parametrize("mod", [track, tt], ids=["track", "token_tracker"])
def test_failed_write_keeps_the_old_state(
    mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text('{"user_message_count": 7}')
    monkeypatch.setattr(mod, "STATE_FILE", state_file)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(mod.os, "replace", boom)
    with pytest.raises(OSError):
        mod.write_state({"user_message_count": 8})
    assert json.loads(state_file.read_text()) == {"user_message_count": 7}
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]
