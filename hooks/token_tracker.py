#!/usr/bin/env python3
"""
Token Tracker — hooks/token_tracker.py (installed beside track.py)

Reads the current session transcript JSONL to compute real token usage.
Called by track.py every TOKEN_REPORT_INTERVAL messages.

Writes results to state.json under "tokens" key.
Prints a formatted summary to stdout for Claude to surface inline.

The cost line is an ESTIMATE at one flat price set for every model. Set your
model's prices (USD per million tokens) and context window in
config.example.sh's CRAFT_PRICE_* / CRAFT_CONTEXT_WINDOW; unset or invalid
values fall back to the defaults below.

Transcript JSONL format (Claude Code):
  Each line is a JSON object. Lines with role "assistant" contain:
    message.usage.input_tokens
    message.usage.output_tokens
    message.usage.cache_creation_input_tokens  (optional)
    message.usage.cache_read_input_tokens       (optional)
"""

import json
import math
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

CLAUDE_DIR = Path(os.environ.get("CLAUDE_DIR", str(Path.home() / ".claude")))
STATE_FILE = CLAUDE_DIR / "session/state.json"


def _env_number(name: str, default: float, minimum: float = 0.0) -> float:
    """A finite number above `minimum` from the environment, else `default`."""
    try:
        value = float(os.environ[name])
    except (KeyError, ValueError):
        return default
    return value if math.isfinite(value) and value > minimum else default


# USD per million tokens. Defaults are one flat set for every model; override
# them for the model you run (see the module docstring).
PRICE_INPUT_PER_M = _env_number("CRAFT_PRICE_INPUT_PER_M", 15.00)
PRICE_OUTPUT_PER_M = _env_number("CRAFT_PRICE_OUTPUT_PER_M", 75.00)
PRICE_CACHE_WRITE_PER_M = _env_number("CRAFT_PRICE_CACHE_WRITE_PER_M", 18.75)
PRICE_CACHE_READ_PER_M = _env_number("CRAFT_PRICE_CACHE_READ_PER_M", 1.50)

# Must be above 1: a value below 1 would be 0 after int() and divide by zero.
CONTEXT_WINDOW = int(_env_number("CRAFT_CONTEXT_WINDOW", 1_000_000, minimum=1))

# Claude Code typically compacts around 80% context usage
COMPACT_THRESHOLD = 0.80


# ── State helpers ─────────────────────────────────────────────────────────────


def read_state() -> dict:
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def write_state(state: dict) -> None:
    # Temp file + rename, so a kill mid-write never leaves an empty state.json.
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(state, indent=2)
    tmp = STATE_FILE.with_name(f"{STATE_FILE.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(data)
        os.replace(tmp, STATE_FILE)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# ── Transcript reader ─────────────────────────────────────────────────────────


def parse_transcript(transcript_path: str) -> dict:
    """
    Read the session transcript JSONL and sum all token usage fields.
    Returns a dict with totals.
    """
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "message_count": 0,
    }

    path = Path(transcript_path)
    if not path.exists():
        return totals

    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Usage can appear at top level or nested in message
                usage = None
                if "usage" in entry:
                    usage = entry["usage"]
                elif "message" in entry and isinstance(entry["message"], dict):
                    usage = entry["message"].get("usage")

                if usage and isinstance(usage, dict):
                    totals["input_tokens"] += usage.get("input_tokens", 0)
                    totals["output_tokens"] += usage.get("output_tokens", 0)
                    totals["cache_creation_input_tokens"] += usage.get(
                        "cache_creation_input_tokens", 0
                    )
                    totals["cache_read_input_tokens"] += usage.get(
                        "cache_read_input_tokens", 0
                    )
                    totals["message_count"] += 1

    except Exception as e:
        print(f"[token_tracker] Transcript read error: {e}", file=sys.stderr)

    return totals


# ── Cost calculator ───────────────────────────────────────────────────────────


def calculate_cost(totals: dict) -> float:
    input_cost = (totals["input_tokens"] / 1_000_000) * PRICE_INPUT_PER_M
    output_cost = (totals["output_tokens"] / 1_000_000) * PRICE_OUTPUT_PER_M
    cache_write = (
        totals["cache_creation_input_tokens"] / 1_000_000
    ) * PRICE_CACHE_WRITE_PER_M
    cache_read = (
        totals["cache_read_input_tokens"] / 1_000_000
    ) * PRICE_CACHE_READ_PER_M
    return input_cost + output_cost + cache_write + cache_read


# ── Context window estimate ───────────────────────────────────────────────────


def context_estimate(totals: dict) -> tuple[int, float]:
    """
    Estimate current context window usage.
    Claude's context includes the full conversation history (input side).
    We use the most recent input_tokens value as the best proxy,
    since it reflects what was sent in the last request.

    Returns (tokens_used_estimate, fraction_used).
    """
    # The last input_tokens value is cumulative for that request.
    # We use total input as a rough upper bound.
    # In practice the active context is the last request's input_tokens.
    # We surface both total consumed and the fraction.
    used = totals["input_tokens"] + totals["output_tokens"]
    fraction = min(used / CONTEXT_WINDOW, 1.0)
    return used, fraction


# ── Formatter ─────────────────────────────────────────────────────────────────


def format_summary(totals: dict, msg_count: int) -> str:
    cost = calculate_cost(totals)
    used_tokens, fraction = context_estimate(totals)

    total_in = totals["input_tokens"]
    total_out = totals["output_tokens"]
    cache_hit = totals["cache_read_input_tokens"]

    # Context bar (20 chars wide)
    filled = int(fraction * 20)
    bar = "█" * filled + "░" * (20 - filled)
    pct = fraction * 100

    # Compact warning
    if fraction >= COMPACT_THRESHOLD:
        ctx_note = f"⚠️  {pct:.0f}% — compaction likely soon"
    elif fraction >= 0.60:
        ctx_note = f"🟡 {pct:.0f}% used"
    else:
        ctx_note = f"🟢 {pct:.0f}% used"

    # Tokens remaining
    remaining = max(0, CONTEXT_WINDOW - used_tokens)

    lines = [
        "┌─ Token Status ──────────────────────────────┐",
        f"  Messages:   {msg_count}",
        f"  Input:      {total_in:,} tokens",
        f"  Output:     {total_out:,} tokens",
        f"  Cost:       ~${cost:.4f} USD (estimate)",
        f"  Context:    [{bar}] {ctx_note}",
        f"  Remaining:  ~{remaining:,} tokens of {CONTEXT_WINDOW:,}",
        "  Resets on:  /compact or new session",
        "└─────────────────────────────────────────────┘",
    ]

    if cache_hit > 0:
        lines.insert(
            -1,
            f"  Cache hit:  {cache_hit:,} tokens (saves ${(cache_hit / 1_000_000) * (PRICE_INPUT_PER_M - PRICE_CACHE_READ_PER_M):.4f})",
        )

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────


def main(transcript_path: str | None = None, silent: bool = False) -> dict:
    """
    Run token tracking. Returns the totals dict.
    If transcript_path is None, tries to read from state.json.
    If silent=True, skips printing (for state-only updates).
    """
    state = read_state()

    # Get transcript path
    if not transcript_path:
        transcript_path = state.get("transcript_path", "")

    totals = {}
    if transcript_path:
        totals = parse_transcript(transcript_path)
        # Cache transcript path for future calls (e.g. from /status)
        state["transcript_path"] = transcript_path
    else:
        # Fall back to last known totals in state
        totals = state.get(
            "tokens",
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "message_count": 0,
            },
        )

    # Update state
    state["tokens"] = totals
    # `.replace` keeps the trailing "Z" the previous naive-utcnow form emitted,
    # so the on-disk string format is byte-identical — only the deprecated
    # utcnow() call changes. tz-aware now() renders "+00:00" without it.
    state["tokens"]["updated_at"] = (
        datetime.now(UTC).isoformat().replace("+00:00", "Z")
    )
    write_state(state)

    if not silent:
        msg_count = state.get("user_message_count", totals.get("message_count", 0))
        print(format_summary(totals, msg_count))

    return totals


if __name__ == "__main__":
    # Can be called directly: python3 token_tracker.py [transcript_path]
    path = sys.argv[1] if len(sys.argv) > 1 else None
    main(transcript_path=path)
