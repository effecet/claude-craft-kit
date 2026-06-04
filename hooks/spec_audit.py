#!/usr/bin/env python3
"""Report open specs from ~/.claude/specs/ by parsing frontmatter.

Status is explicit in each spec's frontmatter (**Status:** <value>). Default
filter hides shipped specs so the output answers "what's still open?" for
session orientation. --all shows everything; --json is machine-readable.

Frontmatter format matches spec_plan_combined.md template:
    **Tier:** <tier>
    **Status:** open | shipped | skipped | superseded
    **Date:** YYYY-MM-DD
    **Origin:** <free text>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

SPECS_DIR = Path.home() / ".claude" / "specs"
_FIELD_RE = re.compile(r"^\*\*(?P<field>\w+):\*\*\s*(?P<value>.+?)\s*$", re.MULTILINE)
VALID_STATUSES = {"open", "shipped", "skipped", "superseded"}
FRONTMATTER_LINE_LIMIT = 30


def parse_frontmatter(path: Path) -> dict[str, str] | None:
    """Extract `**Field:** value` pairs from the first FRONTMATTER_LINE_LIMIT lines.

    Scanning only the top of the file prevents body content (verify sections,
    code blocks, examples) from polluting the field extraction — a later
    `**Status:** shipped` mention anywhere in the doc would otherwise match
    and override the real frontmatter.

    Returns None on read error; caller should skip such files and warn.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        print(f"warn: failed to read {path.name}: {e}", file=sys.stderr)
        return None
    blob = "\n".join(lines[:FRONTMATTER_LINE_LIMIT])
    return {
        m.group("field").lower(): m.group("value").strip()
        for m in _FIELD_RE.finditer(blob)
    }


def collect_specs() -> list[dict[str, str]]:
    if not SPECS_DIR.exists():
        print(f"error: specs dir not found at {SPECS_DIR}", file=sys.stderr)
        sys.exit(1)
    specs = []
    for f in sorted(SPECS_DIR.glob("*.md")):
        fm = parse_frontmatter(f)
        if fm is None:
            continue  # read error already logged to stderr
        raw_status = fm.get("status", "open").lower()
        if raw_status not in VALID_STATUSES:
            print(
                f"warn: {f.name} has unknown status '{raw_status}' — treating as open",
                file=sys.stderr,
            )
            raw_status = "open"
        specs.append(
            {
                "file": f.name,
                "stem": f.stem,
                "tier": fm.get("tier", "?"),
                "status": raw_status,
                "date": fm.get("date", ""),
                "origin": fm.get("origin", ""),
            }
        )
    return specs


def days_since(iso_date: str) -> int | None:
    try:
        d = datetime.strptime(iso_date, "%Y-%m-%d").date()
    except ValueError:
        return None
    # UTC to match the project convention (data-engineering.md) and avoid
    # ±1 day drift for non-UTC users near midnight.
    return (datetime.now(UTC).date() - d).days


def print_table(specs: list[dict[str, str]], show_status: bool) -> None:
    if not specs:
        print("No open specs.")
        return
    specs_sorted = sorted(specs, key=lambda s: s["date"] or "0000-00-00")
    headers = ["date", "tier", "stem", "origin"]
    if show_status:
        headers.insert(2, "status")
    headers.append("age")
    rows = []
    for s in specs_sorted:
        age = days_since(s["date"])
        age_str = "today" if age == 0 else f"{age}d" if age is not None else "?"
        row = [s["date"] or "?", s["tier"], s["stem"], s["origin"] or "—"]
        if show_status:
            row.insert(2, s["status"])
        row.append(age_str)
        rows.append(row)
    widths = [max(len(h), max(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(f"Specs ({len(specs)}):")
    print("  " + fmt.format(*headers))
    print("  " + fmt.format(*["-" * w for w in widths]))
    for r in rows:
        print("  " + fmt.format(*r))


def main() -> None:
    ap = argparse.ArgumentParser(description="Report open specs from ~/.claude/specs/")
    ap.add_argument(
        "--all",
        action="store_true",
        help="Show all specs including shipped/skipped/superseded",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON instead of a human table",
    )
    args = ap.parse_args()

    specs = collect_specs()
    if not args.all:
        specs = [s for s in specs if s["status"] != "shipped"]

    if args.json:
        print(json.dumps(specs, indent=2, default=str))
        return

    print_table(specs, show_status=args.all)


if __name__ == "__main__":
    main()
