#!/usr/bin/env python3
"""
backup_hook.py — optional example backup hook — adapt to your own remote.

When enabled, this hook backs up your Claude config dir (CLAUDE_DIR) by
committing it to whatever git remote you have already configured for that
directory and pushing. It is an OPTIONAL example: it makes no network calls
to any specific provider and stores no secrets — it simply shells out to the
`git` you already use.

Wiring (all via environment variables):
  * BRAIN_ENABLED=true   turn the hook on (default: off → clean no-op exit 0)
  * CLAUDE_DIR           config dir to back up (default: ~/.claude)
  * BACKUP_REMOTE        git remote name to push to (default: "origin")
  * BACKUP_BRANCH        git branch to push (default: "main")
  * BACKUP_DB_URL        OPTIONAL Postgres URL — if set, a pg_dump is written
                         locally under CLAUDE_DIR/backup/db/dump.sql (unset by
                         default → DB backup is skipped entirely)

Setup is up to you: `git init` inside CLAUDE_DIR, add a remote, and make sure
pushes are authenticated (SSH key or credential helper). This hook never
manages tokens. If BRAIN_ENABLED is false the hook does nothing and exits 0.
"""

import logging
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

# ── config (all env-driven, no hardcoded infra) ───────────────────────────────

BRAIN_ENABLED = os.environ.get("BRAIN_ENABLED", "false").lower() == "true"
CLAUDE_DIR = Path(os.environ.get("CLAUDE_DIR", str(Path.home() / ".claude")))
BACKUP_REMOTE = os.environ.get("BACKUP_REMOTE", "origin")
BACKUP_BRANCH = os.environ.get("BACKUP_BRANCH", "main")
BACKUP_DB_URL = os.environ.get("BACKUP_DB_URL", "").strip()

log = logging.getLogger("backup_hook")


# ── git helpers ────────────────────────────────────────────────────────────


def _git(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a git command inside CLAUDE_DIR and return the completed process."""
    return subprocess.run(
        ["git", "-C", str(CLAUDE_DIR), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _is_git_repo() -> bool:
    """True if CLAUDE_DIR is the working tree of a git repo."""
    try:
        result = _git("rev-parse", "--is-inside-work-tree", timeout=10)
        return result.returncode == 0 and result.stdout.strip() == "true"
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


# ── optional DB dump ─────────────────────────────────────────────────────────


def run_pg_dump() -> Path | None:
    """Optionally pg_dump BACKUP_DB_URL to a local file under CLAUDE_DIR.

    Returns the dump path, or None if BACKUP_DB_URL is unset or pg_dump is
    unavailable / fails. The dump is written locally only — it is up to the
    repo's own .gitignore whether it gets committed.
    """
    if not BACKUP_DB_URL:
        return None
    dump_path = CLAUDE_DIR / "backup" / "db" / "dump.sql"
    try:
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["pg_dump", BACKUP_DB_URL, "--no-owner", "--no-privileges"],
            capture_output=True,
            timeout=60,
        )
        if result.returncode != 0:
            log.warning("pg_dump failed: %s", result.stderr.decode(errors="replace").strip())
            return None
        dump_path.write_bytes(result.stdout)
        log.info("DB dump written locally (%d bytes)", len(result.stdout))
        return dump_path
    except FileNotFoundError:
        log.info("pg_dump not found — skipping DB backup")
        return None
    except subprocess.TimeoutExpired:
        log.warning("pg_dump timed out — skipping DB backup")
        return None


# ── orchestration ────────────────────────────────────────────────────────────


def run_backup(dry_run: bool = False) -> dict:
    """Commit and push CLAUDE_DIR to the configured git remote.

    Returns a summary dict. Best-effort: records issues in summary["errors"]
    rather than raising, so the hook never crashes the harness.
    """
    summary: dict = {
        "committed": False,
        "pushed": False,
        "db_backed_up": False,
        "skipped": False,
        "errors": [],
    }

    if not _is_git_repo():
        summary["skipped"] = True
        summary["errors"].append(
            f"{CLAUDE_DIR} is not a git repo — run `git init` and add a remote first"
        )
        return summary

    # Optional DB dump before staging, so it can be picked up by `git add -A`.
    if run_pg_dump() is not None:
        summary["db_backed_up"] = True

    try:
        _git("add", "-A")

        status = _git("status", "--porcelain")
        if not status.stdout.strip():
            log.info("No changes to back up")
            summary["skipped"] = True
            return summary

        timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        message = f"backup: claude config ({timestamp})"

        if dry_run:
            summary["skipped"] = True
            log.info("[DRY RUN] would commit and push: %s", message)
            return summary

        commit = _git("commit", "-m", message)
        if commit.returncode != 0:
            summary["errors"].append(f"commit: {commit.stderr.strip()}")
            return summary
        summary["committed"] = True
        log.info("Committed %s", message)

        push = _git("push", BACKUP_REMOTE, BACKUP_BRANCH, timeout=120)
        if push.returncode != 0:
            summary["errors"].append(f"push: {push.stderr.strip()}")
            log.error("Push failed: %s", push.stderr.strip())
            return summary
        summary["pushed"] = True
        log.info("Pushed to %s/%s", BACKUP_REMOTE, BACKUP_BRANCH)
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        summary["errors"].append(str(e))
        log.error("Backup failed: %s", e)

    return summary


# ── CLI ──────────────────────────────────────────────────────────────────────


def main() -> None:
    # Default OFF — clean no-op exit 0 unless explicitly enabled.
    if not BRAIN_ENABLED:
        sys.exit(0)

    logging.basicConfig(level=logging.INFO, format="[backup_hook] %(message)s")
    dry_run = "--dry-run" in sys.argv

    summary = run_backup(dry_run=dry_run)

    for err in summary.get("errors", []):
        log.error("  %s", err)

    prefix = "[DRY RUN] " if dry_run else ""
    log.info(
        "%scommitted=%s pushed=%s db=%s",
        prefix,
        summary.get("committed"),
        summary.get("pushed"),
        summary.get("db_backed_up"),
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)
