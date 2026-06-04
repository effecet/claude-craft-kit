#!/usr/bin/env python3
"""
UserPromptSubmit hook — ~/.claude/consume_prompt.py
Fires on every user message. Checks for a pending prompt written by
on_idle.sh or on_wrap.sh and surfaces it once, then clears the file.
"""

import json
import sys
from pathlib import Path

PROMPT_FILE = Path.home() / ".claude/session/pending_prompt.json"


def main():
    if not PROMPT_FILE.exists():
        sys.exit(0)

    try:
        content = PROMPT_FILE.read_text().strip()
        if not content:
            sys.exit(0)

        prompt = json.loads(content)
        message = prompt.get("message", "")
        if not message:
            sys.exit(0)

        # Clear immediately so it only fires once
        PROMPT_FILE.write_text("{}")

        # Output the message for Claude to surface in conversation
        print(message)

    except Exception as e:
        print(f"[consume_prompt] Error: {e}", file=sys.stderr)
        sys.exit(0)  # non-fatal


if __name__ == "__main__":
    main()
