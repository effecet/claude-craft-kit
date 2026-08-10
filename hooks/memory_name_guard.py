#!/usr/bin/env python3
"""
memory_name_guard.py — example PreToolUse guard hook.

Fires before a configurable memory-store "remember" tool call. Reads the
tool input JSON from stdin and blocks (exit 2) if the memory `name` exceeds
MAX_WORDS whitespace-separated tokens.

This is an OPTIONAL convention hook, not a correctness gate. It encodes the
"keep names scannable" preference from rules/memory.md — drop it from your
settings.json if you would rather write longer, more descriptive names. The
failure mode actually worth avoiding is a name too vague to recall on, not a
name that runs a few words long.

Scope:
- Fires only on the configured TARGET_TOOL, i.e. at create time.
- Does NOT fire on an `update`-style call. That is a scope choice about where
  the convention is worth enforcing, NOT a claim that renames never happen —
  a backend may well re-slug the filename when `update` changes the name.

Configuration:
- MAX_WORDS — the ceiling, kept in step with rules/memory.md. Raise it
  freely; it is a style preference.
- TARGET_TOOL — the MCP tool name to guard. Override via the
  MEMORY_REMEMBER_TOOL env var to match your own memory-store MCP.
"""

from __future__ import annotations

import json
import os
import sys

MAX_WORDS = 8
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
        f"The name is what you scan in the memory index, so keep it to the\n"
        f"claim itself. Shorten to <={MAX_WORDS} words and move the nuance into\n"
        f"the `description` field, which has no length cap and is what the\n"
        f"index renders after the em dash.\n\n"
        f"This is a style convention, not a hard limit — see rules/memory.md.",
        file=sys.stderr,
    )
    return 2  # blocking; stderr is fed back to Claude


if __name__ == "__main__":
    sys.exit(main())
