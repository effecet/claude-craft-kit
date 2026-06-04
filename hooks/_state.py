"""Shared state schema for session hooks.

Single source of truth for the state.json shape. Used by session_start.py
(new sessions) and track.py (midnight rollover). Adding a new field here
automatically propagates to both callers.
"""

from datetime import UTC, datetime


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
