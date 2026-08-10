"""Tests for the memory-name length guard (hooks/memory_name_guard.py).

Exercised as a real PreToolUse hook: JSON on stdin, exit 2 = block, 0 = allow.
"""

import json
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "memory_name_guard.py"
TOOL = "mcp__memory__remember"  # the default TARGET_TOOL

sys.path.insert(0, str(HOOK.parent))
from memory_name_guard import MAX_WORDS  # noqa: E402

# Derived from MAX_WORDS rather than hardcoded: the threshold is an explicit
# style preference and is meant to be tunable, so raising it must not quietly
# turn "blocks a long name" into a test that no longer blocks anything.
AT_LIMIT = " ".join(f"w{i}" for i in range(MAX_WORDS))
OVER_LIMIT = " ".join(f"w{i}" for i in range(MAX_WORDS + 1))


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


def test_allows_a_name_exactly_at_the_limit():
    assert _run({"tool_name": TOOL, "tool_input": {"name": AT_LIMIT}}) == 0


def test_blocks_a_name_one_word_over_the_limit():
    assert _run({"tool_name": TOOL, "tool_input": {"name": OVER_LIMIT}}) == 2


def test_passes_through_other_tools():
    # Deliberately an over-limit name: this proves the TARGET_TOOL filter is
    # what let it through, not that the name happened to be short enough.
    assert _run({"tool_name": "Bash", "tool_input": {"name": OVER_LIMIT}}) == 0


def test_malformed_input_does_not_block():
    assert _run("not json at all") == 0
