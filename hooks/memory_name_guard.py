#!/usr/bin/env python3
"""
memory_name_guard.py — example PreToolUse guard hook.

Fires before a configurable memory-store "remember" tool call. Reads the
tool input JSON from stdin and blocks (exit 2) if the memory `name` exceeds
MAX_WORDS whitespace-separated tokens.

Why a hook instead of a documented rule:
A short-name convention documented only in prose tends to slip in practice,
producing ugly kebab-truncated filenames like
  feedback_dont-rush-simple-verifications-under-time-pressure.md
Structural enforcement converts "I forgot" into "the harness reminded me".

Scope:
- Fires only on the configured TARGET_TOOL (where the slug is generated).
- Does NOT fire on an `update`-style call that keys on an existing id and
  doesn't rename the file, so a long name there is harmless.

Configuration:
- TARGET_TOOL — the MCP tool name to guard. Override via the
  MEMORY_REMEMBER_TOOL env var to match your own memory-store MCP.
"""

from __future__ import annotations

import json
import os
import sys

MAX_WORDS = 5
TARGET_TOOL = os.environ.get("MEMORY_REMEMBER_TOOL", "mcp__memory__remember")


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # malformed input — let the call proceed; don't break workflows

    tool_name = data.get("tool_name", "")
    if tool_name != TARGET_TOOL:
        return 0

    tool_input = data.get("tool_input", {}) or {}
    name = tool_input.get("name", "")
    if not name or not isinstance(name, str):
        return 0  # no name → MCP itself will reject; not our job

    word_count = len(name.split())
    if word_count <= MAX_WORDS:
        return 0

    print(
        f"MEMORY NAME TOO LONG: {word_count} words (max {MAX_WORDS}).\n"
        f"  name: {name!r}\n\n"
        f"Long names produce ugly kebab-truncated filenames in\n"
        f"  <claude-dir>/projects/<encoded-cwd>/memory/<type>_<slug>.md\n\n"
        f"Shorten to <={MAX_WORDS} words BEFORE calling remember(). The slug is\n"
        f"derived from `name` and is locked at remember() time. Move any extra\n"
        f"context into the `description` field (no length cap) or the body.",
        file=sys.stderr,
    )
    return 2  # blocking; stderr is fed back to Claude


if __name__ == "__main__":
    sys.exit(main())
