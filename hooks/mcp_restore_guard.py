#!/usr/bin/env python3
"""mcp_restore_guard.py — SessionStart self-heal for local (stdio) MCP servers.

WHY: ~/.claude.json (user-scope config) is rewritten whole-file when MCP or
project choices change and on session end. A concurrent session or an unclean
exit can clobber the `mcpServers` block, silently dropping your local stdio
servers while remote connectors survive. This hook re-registers any that went
missing, so your servers are durable across that failure mode.

WHAT: on session start, for each server you list in a config file, if
`claude mcp get <name>` reports it missing, re-register it via `claude mcp add`.
Idempotent — it only acts when a server is absent, so there's no write race
against a healthy config.

CONFIG (opt-in): set MCP_RESTORE_CONFIG, or drop a file at
`$CLAUDE_DIR/mcp-restore.json`. Until that file exists this hook is a silent
no-op. See `mcp-restore.example.json` for the shape — a JSON array of:
    {
      "name": "my-server",
      "scope": "user",                            # optional, default "user"
      "env": {"KEY": "value"},                    # optional
      "command": ["node", "/abs/path/server.js"]  # argv after `claude mcp add … --`
    }

CAVEAT: a re-register may only take effect on the NEXT session (servers spawn
during startup). The win is automatic durability, not in-session repair.
"""

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

CLAUDE_DIR = Path(os.environ.get("CLAUDE_DIR", str(Path.home() / ".claude")))
CONFIG = Path(os.environ.get("MCP_RESTORE_CONFIG", str(CLAUDE_DIR / "mcp-restore.json")))
LOG = CLAUDE_DIR / "mcp_restore_guard.log"


def log(msg: str) -> None:
    try:
        with open(LOG, "a") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except Exception:
        pass


def is_missing(name: str) -> bool:
    """`claude mcp get <name>` exits non-zero when the server isn't registered."""
    try:
        r = subprocess.run(["claude", "mcp", "get", name], capture_output=True, timeout=15)
        return r.returncode != 0
    except Exception:
        return False  # CLI unavailable / errored → don't attempt a "restore"


def restore(entry: dict) -> bool:
    name = entry.get("name")
    command = entry.get("command") or []
    if not name or not command:
        log(f"skip malformed entry: {entry!r}")
        return False
    cmd = ["claude", "mcp", "add", name, "-s", entry.get("scope", "user")]
    for key, value in (entry.get("env") or {}).items():
        cmd += ["-e", f"{key}={value}"]
    cmd += ["--", *command]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=30)
        if r.returncode == 0:
            log(f"{name} re-registered")
            return True
        log(f"{name} re-register FAILED rc={r.returncode}: {r.stderr.decode()[:200]}")
    except Exception as e:
        log(f"{name} re-register error: {e}")
    return False


def main() -> None:
    # Inert unless you've opted in (config present) and the CLI is available.
    if not shutil.which("claude") or not CONFIG.exists():
        return
    try:
        servers = json.loads(CONFIG.read_text())
    except Exception as e:
        log(f"bad config {CONFIG}: {e}")
        return
    if not isinstance(servers, list):
        log("config must be a JSON array of server entries")
        return

    restored = []
    for entry in servers:
        name = (entry or {}).get("name", "")
        if name and is_missing(name) and restore(entry):
            restored.append(name)

    if restored:
        sys.stderr.write(
            f"⚠️  Restored missing local MCP server(s): {', '.join(restored)}. "
            "Restart Claude Code for their tools to load.\n"
        )
        log("restore complete")


if __name__ == "__main__":
    main()
