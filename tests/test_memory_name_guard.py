"""Tests for the memory-name length guard (hooks/memory_name_guard.py).

Exercised as a real PreToolUse hook: JSON on stdin, exit 2 = block, 0 = allow.
"""

import json
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "memory_name_guard.py"
TOOL = "mcp__memory__remember"  # the default TARGET_TOOL


def _run(payload) -> int:
    r = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload) if not isinstance(payload, str) else payload,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return r.returncode


def test_allows_short_name():
    assert _run({"tool_name": TOOL, "tool_input": {"name": "MCP self heal hook"}}) == 0


def test_allows_exactly_five_words():
    assert _run({"tool_name": TOOL, "tool_input": {"name": "one two three four five"}}) == 0


def test_blocks_long_name():
    assert _run({"tool_name": TOOL, "tool_input": {"name": "this memory name has way too many words"}}) == 2


def test_passes_through_other_tools():
    assert _run({"tool_name": "Bash", "tool_input": {"name": "a b c d e f g h"}}) == 0


def test_malformed_input_does_not_block():
    assert _run("not json at all") == 0
