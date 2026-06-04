#!/usr/bin/env python3
"""
UserPromptSubmit hook — proactive memory recall (OPTIONAL example).

On each user prompt, this hook extracts keywords, queries a memory backend
for relevant memories, and surfaces the top matches as system-reminder
context. It is an OPTIONAL example you wire to your OWN memory store — for
instance a memory backend like the companion public "memory-persistor"
project, or any Postgres database holding an `entities` table with `name`,
`type`, `observations`, and `tier` columns.

Wiring:
  * Set BRAIN_ENABLED=true to turn the hook on (default: off).
  * Set DATABASE_URL to a Postgres connection string for your memory store.
  * Optionally set CLAUDE_DIR to override the config dir (default: ~/.claude).

If BRAIN_ENABLED is false OR DATABASE_URL is unset, the hook is a clean
no-op and exits 0. It also skips short messages (<20 chars) and greetings.
Non-fatal everywhere: exits 0 on any error.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────
MAX_RESULTS = 2
MIN_MESSAGE_LENGTH = 20

BRAIN_ENABLED = os.environ.get("BRAIN_ENABLED", "false").lower() == "true"
CLAUDE_DIR = Path(os.environ.get("CLAUDE_DIR", str(Path.home() / ".claude")))


def resolve_connection_string() -> str | None:
    """Return the Postgres connection string for your memory store, or None.

    Read from the DATABASE_URL environment variable. There is no hardcoded
    host, project, or database name — point it at whatever backend you run.
    """
    env_url = os.environ.get("DATABASE_URL", "").strip()
    return env_url or None


def _parse_rows(stdout: str) -> list[dict]:
    memories = []
    for line in stdout.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 4:
            memories.append({
                "name": parts[0], "type": parts[1],
                "observations": parts[2], "tier": parts[3],
            })
    return memories

STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "need", "must",
    "i", "me", "my", "we", "our", "you", "your", "he", "she", "it",
    "they", "them", "their", "this", "that", "these", "those",
    "and", "but", "or", "nor", "not", "so", "if", "then", "than",
    "to", "of", "in", "on", "at", "by", "for", "with", "from",
    "up", "out", "about", "into", "over", "after", "before",
    "what", "which", "who", "whom", "how", "when", "where", "why",
    "all", "each", "every", "both", "few", "more", "most", "some",
    "any", "no", "just", "also", "very", "too", "quite", "rather",
    "hello", "hi", "hey", "thanks", "thank", "please", "yes", "ok", "okay", "sure", "right", "well", "let", "lets", "want",
    "like", "make", "use", "get", "go", "see", "look", "check",
    "know", "think", "try", "give", "take", "come", "say", "tell",
})

GREETING_PATTERN = re.compile(
    r"^\s*(hi|hello|hey|good morning|good afternoon|good evening|"
    r"goodbye|bye)\s*[!.?]*\s*$",
    re.IGNORECASE,
)


def extract_keywords(text: str) -> list[str]:
    """Extract meaningful keywords from user message."""
    cleaned = re.sub(r"[^\w\s-]", " ", text.lower())
    words = cleaned.split()
    keywords = [w for w in words if w not in STOPWORDS and len(w) > 2]
    return keywords[:8]


def sanitize_keyword(keyword: str) -> str:
    """Strip anything non-alphanumeric to prevent SQL injection."""
    return re.sub(r"[^a-z0-9]", "", keyword)


def query_memories(keywords: list[str]) -> list[dict]:
    """Query the configured memory store for relevant memories.

    Uses `psql` against DATABASE_URL. Returns [] when no backend is
    configured or the query fails — the hook is best-effort.
    """
    if not keywords:
        return []

    sanitized = [sanitize_keyword(k) for k in keywords]
    sanitized = [k for k in sanitized if k]
    if not sanitized:
        return []

    tsquery = " & ".join(sanitized)

    sql = (
        "SELECT name, type, "
        "LEFT(observations, 200) as obs, tier "
        "FROM entities "
        "WHERE to_tsvector('english', "
        "COALESCE(name, '') || ' ' || COALESCE(observations, '')) "
        f"@@ to_tsquery('english', '{tsquery}') "
        "ORDER BY ts_rank("
        "to_tsvector('english', COALESCE(name, '') || ' ' || COALESCE(observations, '')), "
        f"to_tsquery('english', '{tsquery}')) DESC "
        f"LIMIT {MAX_RESULTS};"
    )

    conn = resolve_connection_string()
    if not conn:
        return []

    try:
        result = subprocess.run(
            ["psql", conn, "-t", "-A", "-F", "\t", "-c", sql],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return _parse_rows(result.stdout)
    except Exception:
        pass
    return []


def main() -> None:
    # Default OFF — clean no-op unless explicitly enabled with a backend.
    if not BRAIN_ENABLED or not resolve_connection_string():
        return

    hook_data = {}
    try:
        raw = sys.stdin.read()
        if raw.strip():
            hook_data = json.loads(raw)
    except Exception:
        return

    prompt = hook_data.get("prompt", "")

    if len(prompt.strip()) < MIN_MESSAGE_LENGTH:
        return
    if GREETING_PATTERN.match(prompt):
        return

    keywords = extract_keywords(prompt)
    if not keywords:
        return

    memories = query_memories(keywords)
    if not memories:
        return

    lines = ["Relevant memories from your knowledge base:"]
    for m in memories:
        lines.append(f"- [{m['tier']}] {m['name']} ({m['type']}): {m['observations']}")

    print("\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)
