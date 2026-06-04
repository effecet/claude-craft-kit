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
    if wrap_up_ran or wrap_up_prompted or message_count < MIN_MESSAGES_FOR_WRAP_UP:
        # Run optional backup after wrap-up (best-effort, non-blocking)
        if wrap_up_ran and BACKUP_SCRIPT is not None and BACKUP_SCRIPT.exists():
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
            f"If they decline → update {STATE_FILE} with "
            "wrap_up_prompted: true and continue the conversation normally. "
            "Do NOT run wrap-up. "
            "This reminder fires at most once per session."
        ),
    }
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Non-fatal — if the hook fails, let Claude stop normally
        sys.exit(0)
