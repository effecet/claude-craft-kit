#!/usr/bin/env python3
"""memory_name_guard.py — RETIRED; kept as a no-op so old settings keep working.

An earlier version of this kit blocked long memory names at create time. It is
retired (see rules/memory.md § Enforcement). This stub exists only so that a
settings.json still wiring it as a PreToolUse hook does not block every memory
write: a missing script exits 2, which a PreToolUse hook reports as "block".
Remove the hook entry from your settings.json; this file will be deleted in a
later release.
"""

import sys

if __name__ == "__main__":
    sys.exit(0)
