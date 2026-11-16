"""Shared state schema for session hooks.

Single source of truth for the state.json shape (make_fresh_state, used by
session_start.py for new sessions and track.py for the midnight rollover) and
for how hooks write it (write_state_atomic, used by every hook that writes it).
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path


def write_state_atomic(path: Path, state: dict) -> None:
    """Write `state` to `path` as JSON via a temp file + rename.

    Every HOOK that writes state.json goes through this (session_start,
    track, token_tracker, stop_gate, on_idle.sh), so a hook killed mid-write leaves
    the old file, never an empty or half-written one. The temp name carries
    the PID, so two hooks writing at once never share a temp file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(state, indent=2)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _now_utc() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def make_fresh_state(preserved: dict | None = None) -> dict:
    """Return a canonical fresh state.json dict.

    Args:
        preserved: optional dict of fields to carry over from a prior state
                   (e.g. transcript_path, cwd from a midnight rollover).
                   Only known keys are carried; unknown keys are dropped.
    """
    now = _now_utc()
    state = {
        # Identity / lifecycle
        "session_id": now,
        "user_message_count": 0,
        "last_active": now,
        # Wrap-up flags
        "wrap_up_ran": False,
        "wrap_up_prompted": False,
        "stop_gate_prompted": False,
        "threshold_prompted": False,
        "idle_prompted": False,
        "last_idle_trigger": 0,
        # Bookkeeping
        "breadcrumb_written": False,
    }
    # Carry over live-session fields that describe the current session,
    # not yesterday's flags.
    if preserved:
        for key in ("transcript_path", "cwd"):
            if preserved.get(key):
                state[key] = preserved[key]
    return state
