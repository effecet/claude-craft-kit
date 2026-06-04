#!/usr/bin/env python3
"""
validate_tier.py — Determine which validation tier a diff requires.

Reads `git diff` for the given scope, counts changed LOC, scans for sensitive
path patterns and forbidden code markers, and prints the recommended validation
tier per rules/development.md (Skip / Light / Full), or BLOCK if forbidden
markers are present.

Usage:
    validate_tier.py [scope] [--json]

scope (optional, default HEAD):
    HEAD          diff vs HEAD (working tree changes)
    --staged      diff staged changes only
    <ref>         diff vs a specific branch or commit (e.g. main, abc123)

Exit codes:
    0  Skip / Light / Full (advisory — caller decides)
    2  BLOCK (forbidden markers present — must fix before any gate)
    1  Error (no git repo, bad scope, etc.)
"""

import json
import re
import subprocess
import sys

# Sensitive path keywords from rules/development.md — auto-escalate to Full tier.
# Common words (auth, session, schema, migration, etc.) are anchored by segment
# boundaries — `/`, `_`, `.`, `-`, or string end — to avoid overmatching
# innocuous filenames like `authority.py`, `sessionizer.py`, `schema_helpers.py`.
# Rare/distinctive words (stripe, password, rbac) use plain substring since
# false positives are unlikely.
#
# Each entry is (friendly_name, compiled_regex). The friendly name is what
# shows up in reports and what downstream consumers (tests, JSON output) see.
_BOUND = r"(?:^|[/_.\-])"
_BOUND_END = r"(?:$|[/_.\-])"


def _bounded(word: str) -> re.Pattern[str]:
    return re.compile(rf"{_BOUND}{word}{_BOUND_END}", re.IGNORECASE)


def _sub(word: str) -> re.Pattern[str]:
    return re.compile(word, re.IGNORECASE)


# Template env filename suffixes — checked in-sync by the `.env` sensitive
# pattern AND the SKIP_PATTERNS entry below. Adding a suffix here propagates
# to both sites; drifting them would silently restore the false-escalation bug.
# Underscore prefix is a convention hint — the test suite iterates this tuple
# so coverage auto-extends when a new suffix is added. A rename would surface
# as AttributeError in tests, which is the intended loud-failure behavior.
_TEMPLATE_SUFFIXES: tuple[str, ...] = ("example", "template", "sample", "dist")
_TEMPLATE_ALT = "|".join(_TEMPLATE_SUFFIXES)

SENSITIVE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("auth", _bounded("auth")),
    ("login", _bounded("login")),
    ("session", _bounded("session")),
    ("token", _bounded("token")),
    ("password", _sub("password")),
    ("payment", _sub("payment")),
    ("stripe", _sub("stripe")),
    ("billing", _sub("billing")),
    ("migration", re.compile(rf"{_BOUND}migration", re.IGNORECASE)),
    ("schema", _bounded("schema")),
    ("alter", _bounded("alter")),
    ("secret", _sub("secret")),
    ("credential", _sub("credential")),
    # Real .env files escalate; template variants (.env.example, .env.template,
    # .env.sample, .env.dist, and multi-dot forms like .env.local.example)
    # hold placeholders and must not. Keep in sync with SKIP_PATTERNS below.
    (
        ".env",
        re.compile(
            rf"(^|/)\.env(?![\w.\-]*\.({_TEMPLATE_ALT})$)",
            re.IGNORECASE,
        ),
    ),
    ("security", _sub("security")),
    ("permission", _sub("permission")),
    ("rbac", _bounded("rbac")),
    ("rls", _bounded("rls")),
]

# File-level exemption (different from `.env`-style in-pattern lookahead in
# SENSITIVE_PATTERNS): an exempt path is dropped from ALL sensitive checks,
# not just one. Each entry must be paired with tests proving the false
# positive is cured AND a near-miss path still escalates.
SENSITIVE_EXEMPT_PATHS: list[re.Pattern[str]] = [
    re.compile(r"(^|/)\.claude/session/[^/]+\.json$"),
    re.compile(r"(^|/)claude-config/skills/skillgenome/session-log\.json$"),
    # Calendar/booking session files (schedule-session.*, booking-session.*,
    # calendar-session.*) are NOT auth sessions and must not trigger the
    # `session` sensitive escalation. Example origin: a calendar availability
    # handler (schedule-session.js) once auto-escalated a 6-LOC CSS-class
    # rename to Full tier.
    re.compile(r"(^|/)(schedule|booking|calendar)-session\.[a-z]+$"),
]

# Forbidden code markers — block before any gate runs
FORBIDDEN_MARKERS = ["TODO", "FIXME", "FIX", "HACK", "TEMP", "XXX"]

# Skip-tier file patterns: config / docs only — no testable code
SKIP_PATTERNS = [
    r"\.md$",
    r"\.txt$",
    r"\.json$",
    r"\.yaml$",
    r"\.yml$",
    r"\.toml$",
    r"\.ini$",
    r"\.cfg$",
    r"\.gitignore$",
    rf"\.env[\w.\-]*\.({_TEMPLATE_ALT})$",
    r"^docs/",
    r"/docs/",
    r"^README",
    r"/README",
]

# Marker-scan exemptions: files that legitimately contain forbidden marker
# strings as DATA (not as leftover code markers). validate_tier.py defines the
# marker list; its test file uses them as fixture input. Both would otherwise
# cause the scanner to block on itself. Distinct from SKIP_PATTERNS because
# these files ARE real code and should affect tier selection normally.
MARKER_SCAN_EXEMPT = [
    r"(^|/)validate_tier\.py$",
    r"(^|/)test_validate_tier\.py$",
]

LIGHT_THRESHOLD = 30  # LOC — boundary between Light and Full tiers


def get_diff(scope: str) -> tuple[list[str], dict[str, list[str]], int]:
    """Return (changed_files, added_lines_by_file, total_loc_changed).

    added_lines_by_file maps each file path to the list of added lines from
    that file. Files are listed in the order they appear in the diff.
    """
    if scope == "--staged":
        cmd = ["git", "diff", "--staged"]
    else:
        cmd = ["git", "diff", scope]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError:
        print("error: git not found in PATH", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(
            f"error: git diff failed ({e.returncode}): {e.stderr.strip()}",
            file=sys.stderr,
        )
        sys.exit(1)
    files: list[str] = []
    added: dict[str, list[str]] = {}
    loc = 0
    current: str | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
            files.append(current)
            added.setdefault(current, [])
        elif (
            line.startswith("+") and not line.startswith("+++") and current is not None
        ):
            added[current].append(line[1:])
            loc += 1
        elif line.startswith("-") and not line.startswith("---"):
            loc += 1
    return files, added, loc


def all_files_skippable(files: list[str]) -> bool:
    """True iff every changed file matches a skip pattern (config/docs only)."""
    if not files:
        return True
    return all(any(re.search(p, f) for p in SKIP_PATTERNS) for f in files)


def matched_sensitive_paths(files: list[str]) -> list[str]:
    """Return the unique set of sensitive pattern NAMES that matched any file path."""
    hits: set[str] = set()
    for f in files:
        if any(p.search(f) for p in SENSITIVE_EXEMPT_PATHS):
            continue
        for name, pattern in SENSITIVE_PATTERNS:
            if pattern.search(f):
                hits.add(name)
    return sorted(hits)


def find_forbidden_markers(added: dict[str, list[str]]) -> list[str]:
    """Return list of 'MARKER in file: <line snippet>' for any forbidden marker hit.

    Only scans files that DON'T match SKIP_PATTERNS — markdown docs, JSON/YAML
    configs, .gitignore etc. legitimately mention 'TODO' as prose. Forbidden
    markers apply to code, not documentation about code.

    Also skips MARKER_SCAN_EXEMPT files — the scanner's own source and its
    test file, which legitimately contain marker strings as data.
    """
    hits: list[str] = []
    for file, lines in added.items():
        if any(re.search(p, file) for p in SKIP_PATTERNS):
            continue  # docs/config — skip marker scan
        if any(re.search(p, file) for p in MARKER_SCAN_EXEMPT):
            continue  # scanner's own source/tests — marker strings are data
        for line in lines:
            for marker in FORBIDDEN_MARKERS:
                if re.search(rf"\b{marker}\b", line):
                    hits.append(f"{marker} in {file}: {line.strip()[:80]}")
                    break  # one finding per line
    return hits


def recommend_tier(files: list[str], added: dict[str, list[str]], loc: int) -> dict:
    """Decide tier given the diff. Marker block > Skip > sensitive Full > LOC threshold.

    Return schema is stable across all tiers — `markers` and `sensitive_patterns`
    are always present (possibly empty lists), so JSON consumers can rely on a
    single shape instead of probing for keys.
    """
    base = {
        "loc": loc,
        "files": len(files),
        "markers": [],
        "sensitive_patterns": [],
    }
    markers = find_forbidden_markers(added)
    if markers:
        return {
            **base,
            "tier": "BLOCK",
            "reason": f"{len(markers)} forbidden marker(s) found — resolve before any gate",
            "markers": markers[:10],
        }

    if all_files_skippable(files):
        return {
            **base,
            "tier": "Skip",
            "reason": "All changed files match config/docs patterns — no testable code",
        }

    sensitive = matched_sensitive_paths(files)
    if sensitive:
        return {
            **base,
            "tier": "Full",
            "reason": f"Sensitive paths detected ({', '.join(sensitive)}) — auto-escalate",
            "sensitive_patterns": sensitive,
        }

    if loc < LIGHT_THRESHOLD:
        return {
            **base,
            "tier": "Light",
            "reason": f"{loc} LOC changed (< {LIGHT_THRESHOLD}), no sensitive paths",
        }

    return {
        **base,
        "tier": "Full",
        "reason": f"{loc} LOC changed (≥ {LIGHT_THRESHOLD})",
    }


def print_human(result: dict) -> None:
    icons = {"Skip": "⏭️ ", "Light": "🟡", "Full": "🔴", "BLOCK": "🛑"}
    icon = icons.get(result["tier"], "❓")
    print(f"{icon}  Tier: {result['tier']}")
    print(f"   {result['reason']}")
    print(f"   Files: {result['files']}, LOC: {result['loc']}")
    if result.get("markers"):
        print("   Markers:")
        for m in result["markers"]:
            print(f"     - {m}")
    if result.get("sensitive_patterns"):
        print(f"   Sensitive: {', '.join(result['sensitive_patterns'])}")


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--json"]
    json_mode = "--json" in sys.argv
    scope = args[0] if args else "HEAD"

    files, added, loc = get_diff(scope)
    result = recommend_tier(files, added, loc)

    if json_mode:
        print(json.dumps(result, indent=2))
    else:
        print_human(result)

    sys.exit(2 if result["tier"] == "BLOCK" else 0)


if __name__ == "__main__":
    main()
