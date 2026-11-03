#!/usr/bin/env python3
"""
Stop hook — stop_gate.py

Fires when Claude is about to stop responding.
If the session was substantial and wrap-up hasn't run, blocks the stop
and instructs Claude to run /wrap-up first.

Returns JSON to stdout:
  - {"decision": "block", "reason": "..."} to prevent stopping
  - {} or nothing to allow stopping normally

Configuration (env):
  - CLAUDE_DIR    base Claude config dir (default: ~/.claude)
  - BACKUP_SCRIPT optional path to a best-effort backup script run after
                  wrap-up. If unset/missing, the backup step is skipped.
  - PYTHON_BIN    interpreter used to run BACKUP_SCRIPT (default: python3)
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

CLAUDE_DIR = Path(os.environ.get("CLAUDE_DIR", str(Path.home() / ".claude")))
STATE_FILE = CLAUDE_DIR / "session/state.json"
_backup_env = os.environ.get("BACKUP_SCRIPT", "")
BACKUP_SCRIPT = Path(_backup_env) if _backup_env else None
PYTHON = os.environ.get("PYTHON_BIN", "python3")
MIN_MESSAGES_FOR_WRAP_UP = 10  # trivial sessions don't need wrap-up


def main() -> None:
    # Parse hook input from stdin
    hook_data = {}
    try:
        raw = sys.stdin.read()
        if raw.strip():
            hook_data = json.loads(raw)
    except Exception:
        pass

    # Prevent infinite loop: if stop_gate already fired once, let it through
    if hook_data.get("stop_hook_active", False):
        return

    # Read session state
    try:
        if STATE_FILE.exists():
            state = json.loads(STATE_FILE.read_text())
        else:
            return  # no state = no session tracking = let it stop
    except Exception:
        return

    wrap_up_ran = state.get("wrap_up_ran", False)
    wrap_up_prompted = state.get("wrap_up_prompted", False)
    message_count = state.get("user_message_count", 0)

    # If wrap-up already ran, was already offered (and presumably declined), or
    # the session is trivial, let it stop. wrap_up_prompted=True means the user
    # has already been asked this session — don't nag them again.
    stop_gate_prompted = state.get("stop_gate_prompted", False)
    if (
        wrap_up_ran
        or wrap_up_prompted
        or stop_gate_prompted
        or message_count < MIN_MESSAGES_FOR_WRAP_UP
    ):
        # Run optional backup after wrap-up (best-effort, non-blocking)
        if wrap_up_ran and BACKUP_SCRIPT is not None and BACKUP_SCRIPT.exists():
            if shutil.which(PYTHON) is None:
                # Popen would raise into a swallowed exception with output at
                # DEVNULL, so the backup would silently never run. Say so.
                print(
                    f"[stop_gate] backup skipped: PYTHON_BIN {PYTHON!r} is not executable",
                    file=sys.stderr,
                )
            else:
                try:
                    subprocess.Popen(
                        [PYTHON, str(BACKUP_SCRIPT)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except Exception:
                    pass  # backup is best-effort
        return

    # Block the stop — surface a REMINDER (not a command). Ask the user
    # whether to wrap up or keep going; never force wrap-up execution.
    #
    # Claim the prompt BEFORE emitting it. Relying on the model to record that
    # it asked meant a model that asked and kept working (e.g. while background
    # agents ran) got the identical reminder on the next stop. The flag is this
    # hook's own (stop_gate_prompted), not wrap_up_prompted: that key also arms
    # the message-threshold and goodbye reminders in track.py, and claiming it
    # here would silently disarm both. state.json resumes for every session on
    # the same day, so this fires at most once per day, not per session.
    state["stop_gate_prompted"] = True
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception:
        pass  # best-effort — a failed write costs one extra reminder, nothing more
    skill_path = CLAUDE_DIR / "skills/wrap-up/SKILL.md"
    result = {
        "decision": "block",
        "reason": (
            f"REMINDER: session has {message_count} messages and wrap-up hasn't "
            "run yet. Before stopping, ASK the user ONCE: "
            "\"Want me to run the wrap-up sequence now (ship work, capture "
            "memories, review), or just close out / keep working?\" Then wait "
            "for their answer. "
            f"If they confirm → invoke the wrap-up skill "
            f"({skill_path}, trigger: session_end). "
            "If they decline → just continue the conversation normally; this "
            "hook has already recorded that you asked, so it will not re-fire. "
            "Do NOT run wrap-up. "
            "This reminder fires at most once per day (session state resumes "
            "for every session started the same day)."
        ),
    }
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Non-fatal — if the hook fails, let Claude stop normally
        sys.exit(0)
