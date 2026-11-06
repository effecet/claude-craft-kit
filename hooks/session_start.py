#!/usr/bin/env python3
"""
SessionStart hook — ~/.claude/session_start.py
Runs once when Claude Code starts a session.

Responsibilities:
  1. Initialize or resume state.json
  2. Collect context (git status, pending items, recent learnings)
  3. Write brief.md for Claude to read and present as orientation
"""

import inspect
import json
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.parse
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple, cast

from _state import make_fresh_state
from common import resolve_connection_string

CLAUDE_DIR = Path(os.environ.get("CLAUDE_DIR", str(Path.home() / ".claude")))
STATE_FILE = CLAUDE_DIR / "session/state.json"
BRIEF_FILE = CLAUDE_DIR / "session/brief.md"
PENDING_FILE = CLAUDE_DIR / "pending.md"
LEARNINGS = CLAUDE_DIR / "LEARNINGS.md"
SPEC_AUDIT = CLAUDE_DIR / "hooks/spec_audit.py"

MACHINE_ID = socket.gethostname().split(".")[0]  # short hostname for commit tags

PENDING_BRIEF_LIMIT = 10  # mirrors PENDING_BRIEF_LIMIT in the backend's config
PENDING_TIMEOUT_S = 5  # session start must not stall on a slow connection

# Claude Code caps a hook's plain stdout at 10,000 characters, measured whole,
# with no setting to raise it. Over the cap the model gets a file path and a
# 2,000-char preview and is NOT asked to read the rest, so whatever comes last
# silently vanishes. The budget leaves room under that for the meter line.
STDOUT_BUDGET = 9_000
STDOUT_OVERFLOW_FILE = CLAUDE_DIR / "session/session_start_full.txt"
_METER_RESERVE = 300
# A single line longer than this is clipped, so it cannot push every alert
# after it out of the budget.
_LINE_CAP = 1_000

# ---- Optional config-checkout checks — every one is silent until configured.
# CRAFT_CONFIG_REPO      git checkout your ~/.claude hooks/scripts deploy from
#                        (enables the deployed-hook drift check).
# CRAFT_CONFIG_SUBDIR    subdirectory of that checkout holding hooks/ and
#                        scripts/ (default: the checkout root, as in this kit).
# CRAFT_CONFIG_BRANCH    branch the live hooks should come from (default: main).
# CRAFT_CANARY_FILE      heartbeat file a scheduled job writes Unix time into;
# CRAFT_CANARY_MAX_AGE_H warn once it is older than this many hours (default 3).
# CRAFT_GIT_HOOKS_DIR    global git hooks dir that core.hooksPath must point at.
CONFIG_REPO = (
    Path(os.path.expanduser(os.environ["CRAFT_CONFIG_REPO"]))
    if os.environ.get("CRAFT_CONFIG_REPO")
    else None
)
CONFIG_SUBDIR = os.environ.get("CRAFT_CONFIG_SUBDIR", "").strip("/")
CONFIG_BRANCH = os.environ.get("CRAFT_CONFIG_BRANCH", "main")
CANARY_FILE = (
    Path(os.path.expanduser(os.environ["CRAFT_CANARY_FILE"]))
    if os.environ.get("CRAFT_CANARY_FILE")
    else None
)
try:
    CANARY_MAX_AGE_H = float(os.environ.get("CRAFT_CANARY_MAX_AGE_H", "3"))
except ValueError:
    CANARY_MAX_AGE_H = 3.0


def _config_dir(root: Path) -> Path:
    """The directory holding hooks/ and scripts/ inside a config checkout."""
    return root / CONFIG_SUBDIR if CONFIG_SUBDIR else root


def normalize_host(host: str) -> str:
    """Canonical host spelling: lowercase, cut at the first dot, keep [a-z0-9_-].

    Pending items can be scoped to hosts (the backend's `applies_to` column),
    which makes visibility a string comparison between the host the backend
    STORED and the host computed here. This MUST match the backend's
    `normalizeHost` exactly; if the two ever disagree, an item stops matching
    its own machine and silently disappears. The character strip is part of
    the rule, not sanitation for one call site.

    Only this side can be pinned here (tests/test_session_start.py carries the
    backend's reference table); the backend pins its own side against the same
    table, so the two meet at the table rather than in one test.
    """
    return re.sub(r"[^a-z0-9_-]", "", host.split(".")[0].lower())


def _host_scope() -> str:
    """SQL predicate: fleet-wide (empty applies_to) or scoped to this host.

    The host is embedded directly because psql does not interpolate `-v`
    variables into `-c` statements. That is safe only because normalize_host
    is an allowlist — no quote, backslash or semicolon survives it.

    Built from MACHINE_ID, which is resolved once at import, so calling this
    for each statement costs only the normalisation.
    """
    host = normalize_host(MACHINE_ID)
    return f"(cardinality(applies_to) = 0 OR '{host}' = ANY(applies_to))"


# The counts are their own statement, not a window function over the items,
# because the hidden count must survive ZERO items in scope here — exactly when
# it matters. `IS TRUE` / `IS NOT TRUE` keep the partition total: a NULL element
# in applies_to makes `= ANY(...)` NULL, and a bare predicate would drop that
# row from both counts.
def _counts_sql() -> str:
    scope = _host_scope()
    return f"""
SELECT COUNT(*) FILTER (WHERE ({scope}) IS TRUE) AS here_open,
       COUNT(*) FILTER (WHERE ({scope}) IS NOT TRUE) AS hidden_by_host
FROM pending
WHERE status = 'open';
"""


# Shared by the scoped and legacy item queries so the two cannot drift.
#
# Raw strings: the E'\t' / E'\n' / E'\r' escapes must reach Postgres as the
# literal two-char sequences, not as real control bytes. Every row separator
# the renderers split on is replaced, so a title can never spill into a second
# row or shift a column.
_TITLE_COL = (
    r"replace(replace(replace(title, E'\t', ' '), E'\n', ' '), E'\r', ' ') AS title"
)
_ORDER_LIMIT = f"""ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
         created_at DESC
LIMIT {PENDING_BRIEF_LIMIT};"""

# category is deliberately NOT selected: the brief only ever renders
# priority + title, so a category column would be dead weight on the wire.
# Adding a column means widening the matching renderer in lockstep:
# _items_sql -> _render_pending (2 columns, after its counts row), and
# PENDING_LEGACY_SQL -> _render_pending_legacy (3 columns).


def _items_sql() -> str:
    return f"""
SELECT priority, {_TITLE_COL}
FROM pending
WHERE status = 'open' AND ({_host_scope()}) IS TRUE
{_ORDER_LIMIT}
"""


# DELETE once every supported backend carries applies_to: this constant,
# _render_pending_legacy, the _is_missing_applies_to retry in
# _pending_from_db, and their tests. A degrade path with no expiry becomes
# permanent scaffolding. It exists because code can reach a machine before the
# matching backend migration does.
PENDING_LEGACY_SQL = f"""
SELECT COUNT(*) OVER () AS total_open, priority, {_TITLE_COL}
FROM pending
WHERE status = 'open'
{_ORDER_LIMIT}
"""

# When false (default), all calls into an optional private "memory brain"
# (git auto-sync, memory decay, remote connectivity checks) are skipped.
# The rest of the hook — state init, git context, briefing — always runs.
# optional: wire your own memory backend here (see docs/WORKFLOW.md)
BRAIN_ENABLED = os.environ.get("BRAIN_ENABLED", "false").lower() == "true"

BRAIN_SYNC_INTERVAL = 3  # auto-push every N sessions
EVOLVE_CHECK_INTERVAL = 5  # check instinct count every N sessions
EVOLVE_MIN_INSTINCTS = 3  # minimum instincts before suggesting /evolve
MEMORY_DECAY_INTERVAL = 1  # decay every session (lightweight)
HOMUNCULUS_DIR = CLAUDE_DIR / "homunculus"


def _find_memory_repo() -> Path:
    """Auto-detect an optional memory-backend repo location.

    Override with the MEMORY_REPO_DIR env var. The default scan locations are
    just conveniences; if nothing is found the returned path simply won't
    exist and brain steps no-op.
    """
    env_dir = os.environ.get("MEMORY_REPO_DIR")
    if env_dir:
        return Path(env_dir)
    for parent in ("Desktop", "Downloads"):
        candidate = Path.home() / parent / "memory-backend"
        if (candidate / ".git").exists():
            return candidate
    return Path.home() / "Desktop" / "memory-backend"  # fallback


MEMORY_REPO_DIR = _find_memory_repo()

# ── State init / resume ───────────────────────────────────────────────────────


def init_state() -> tuple[dict, bool]:
    """Returns (state, resumed). Resumes if state.json is from today.

    state.json schema (canonical — keep in sync with track.py and stop_gate.py):

    Identity / lifecycle:
        session_id (str)              ISO timestamp of session start (date prefix used for day boundary)
        last_active (str)             ISO timestamp updated on every user message
        user_message_count (int)      Count of UserPromptSubmit events this session
        transcript_path (str)         Path to current session's .jsonl transcript
        cwd (str)                     Working directory of current session

    Wrap-up state (set by track.py / stop_gate.py / wrap-up skill):
        wrap_up_ran (bool)            True after wrap-up skill completes Phase 5
        wrap_up_prompted (bool)       True after the user has been ASKED about wrap-up (any trigger)
        stop_gate_prompted (bool)     True once stop_gate.py has shown its reminder (its own key,
                                      so it never disarms the track.py reminders)
        threshold_prompted (bool)     True after the message-count threshold prompt fired
        idle_prompted (bool)          True after the idle-watcher prompted
        last_idle_trigger (int)       Unix timestamp of last idle trigger fire

    Bookkeeping:
        breadcrumb_written (bool)     True after track.py wrote to LEARNINGS.md this session

    Cross-session counters (carried across sessions, not per-session flags):
        brain_session_count (int)            Backed-up sessions counter
        memory_decay_session_count (int)     Sessions since last decay run
        evolve_session_count (int)           Sessions since last /evolve run

    The "_prompted" flags are sprawled by trigger source (threshold/idle/wrap_up).
    They share one semantic — "have we asked already?" — and `wrap_up_prompted`
    is the OR of all of them. Future cleanup: collapse into a single
    `wrap_up_offered_at` timestamp. For now they're kept separate so the existing
    logic in track.py / stop_gate.py keeps working.
    """
    # UTC for both — session_id is stored in UTC, so day comparison must also
    # be UTC or the two detectors (init_state here and
    # track._archive_and_reset_if_day_changed) disagree around midnight UTC.
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
            if state.get("session_id", "")[:10] == today:
                state["last_active"] = now
                write_state(state)
                return state, True
        except Exception:
            pass

    state = make_fresh_state()
    write_state(state)
    return state, False


def write_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Git context ───────────────────────────────────────────────────────────────


def git_summary() -> list[str]:
    cwd = Path(os.getcwd())
    items = []
    seen = set()

    candidates = [cwd]
    for d in cwd.iterdir():
        if d.name.startswith(".") or not d.is_dir():
            continue
        try:
            candidates.append(d)
            for sub in d.iterdir():
                if sub.name.startswith(".") or not sub.is_dir():
                    continue
                candidates.append(sub)
        except PermissionError:
            continue

    for d in candidates:
        try:
            if not d.is_dir() or not (d / ".git").exists():
                continue
        except PermissionError:
            continue
        key = str(d.resolve())
        if key in seen:
            continue
        seen.add(key)

        try:
            status = subprocess.run(
                ["git", "-C", str(d), "status", "--short"],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
            branch = subprocess.run(
                ["git", "-C", str(d), "branch", "--show-current"],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()

            changed = len([l for l in status.splitlines() if l.strip()])
            label = d.name + (" (cwd)" if d == cwd else "")
            status_str = f"{changed} uncommitted change(s)" if changed else "clean"
            items.append(f"- `{label}` [{branch}] — {status_str}")
        except Exception:
            continue

    return items or ["No repos found in current directory tree."]


# ── Brain sync (auto-commit + push to git remote) ────────────────────────────


def brain_sync(state: dict) -> list[str]:
    """
    Every BRAIN_SYNC_INTERVAL sessions, auto-commit changed tracked files
    in CLAUDE_DIR and push to origin. Warns about untracked files.
    Returns status messages for the brief.

    Gated behind BRAIN_ENABLED — no-op when the optional brain is disabled.
    """
    msgs: list[str] = []
    if not BRAIN_ENABLED:
        return msgs

    # Only run if ~/.claude is a git repo
    git_dir = CLAUDE_DIR / ".git"
    if not git_dir.exists():
        return msgs

    count = state.get("brain_session_count", 0) + 1
    state["brain_session_count"] = count

    # Check for untracked files first (always report, even if not a sync session)
    try:
        untracked = subprocess.run(
            [
                "git",
                "-C",
                str(CLAUDE_DIR),
                "ls-files",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        if untracked:
            files = untracked.splitlines()
            msgs.append(
                f"🧠 Brain: {len(files)} new untracked file(s): {', '.join(files[:5])}"
                + (f" (+{len(files) - 5} more)" if len(files) > 5 else "")
            )
    except Exception:
        pass

    # Only sync every N sessions
    if count % BRAIN_SYNC_INTERVAL != 0:
        remaining = BRAIN_SYNC_INTERVAL - (count % BRAIN_SYNC_INTERVAL)
        msgs.append(f"🧠 Brain: next auto-sync in {remaining} session(s)")
        return msgs

    try:
        # Stage changed tracked files only (not untracked)
        subprocess.run(
            ["git", "-C", str(CLAUDE_DIR), "add", "-u"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        # Check if there's anything to commit
        diff = subprocess.run(
            ["git", "-C", str(CLAUDE_DIR), "diff", "--cached", "--stat"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()

        if not diff:
            msgs.append("🧠 Brain: sync check — nothing changed since last commit")
            return msgs

        # Commit
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        subprocess.run(
            [
                "git",
                "-C",
                str(CLAUDE_DIR),
                "commit",
                "-m",
                f"[brain] auto-sync: session {count} ({ts}) [{MACHINE_ID}]",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )

        # Push
        result = subprocess.run(
            ["git", "-C", str(CLAUDE_DIR), "push"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            msgs.append(f"🧠 Brain: auto-synced to remote (session {count})")
        else:
            msgs.append(
                f"🧠 Brain: commit OK but push failed — {result.stderr.strip()[:80]}"
            )

    except Exception as e:
        msgs.append(f"🧠 Brain: sync error — {e}")

    return msgs


# ── Memory decay (thermal decay + snapshot) ──────────────────────────────────


def memory_decay(state: dict) -> list[str]:
    """
    Every MEMORY_DECAY_INTERVAL sessions, run an optional memory decay script.
    Decays temperature on stale entities and exports JSON snapshots.

    Gated behind BRAIN_ENABLED — no-op when the optional brain is disabled.
    The decay script is part of your own memory backend, if any.
    # optional: wire your own memory backend here (see docs/WORKFLOW.md)
    """
    msgs: list[str] = []
    if not BRAIN_ENABLED:
        return msgs

    decay_script = MEMORY_REPO_DIR / "scripts" / "memory-decay.py"
    if not decay_script.exists():
        return msgs

    count = state.get("memory_decay_session_count", 0) + 1
    state["memory_decay_session_count"] = count

    if count % MEMORY_DECAY_INTERVAL != 0:
        return msgs

    try:
        result = subprocess.run(
            [sys.executable, str(decay_script)],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(MEMORY_REPO_DIR),
        )
        output = result.stdout.strip()
        if output:
            msgs.append(output)
        if result.returncode != 0 and result.stderr.strip():
            msgs.append(f"🧊 Memory decay warning: {result.stderr.strip()[:80]}")
    except Exception as e:
        msgs.append(f"🧊 Memory decay error: {e}")

    return msgs


# ── Evolve check (instinct accumulation) ─────────────────────────────────────


def evolve_check(state: dict) -> list[str]:
    """
    Every EVOLVE_CHECK_INTERVAL sessions, count instincts across all projects
    and global scope. If >= EVOLVE_MIN_INSTINCTS, suggest running /evolve.
    """
    msgs: list[str] = []

    count = state.get("evolve_session_count", 0) + 1
    state["evolve_session_count"] = count

    if count % EVOLVE_CHECK_INTERVAL != 0:
        return msgs

    # Count instinct files across global + all projects
    total = 0
    by_scope: dict[str, int] = {}

    # Global instincts
    global_dir = HOMUNCULUS_DIR / "instincts" / "personal"
    if global_dir.exists():
        n = sum(
            1
            for f in global_dir.iterdir()
            if f.suffix in (".yaml", ".yml", ".md") and f.is_file()
        )
        if n:
            by_scope["global"] = n
            total += n

    # Project-scoped instincts
    projects_dir = HOMUNCULUS_DIR / "projects"
    if projects_dir.exists():
        for proj in projects_dir.iterdir():
            if not proj.is_dir():
                continue
            personal = proj / "instincts" / "personal"
            if personal.exists():
                n = sum(
                    1
                    for f in personal.iterdir()
                    if f.suffix in (".yaml", ".yml", ".md") and f.is_file()
                )
                if n:
                    by_scope[proj.name[:8]] = n
                    total += n

    if total >= EVOLVE_MIN_INSTINCTS:
        breakdown = ", ".join(f"{k}: {v}" for k, v in by_scope.items())
        msgs.append(
            f"🧬 Instincts: {total} accumulated ({breakdown}) — ready for /evolve"
        )
    elif total > 0:
        remaining = EVOLVE_MIN_INSTINCTS - total
        msgs.append(
            f"🧬 Instincts: {total} so far — {remaining} more needed for /evolve"
        )

    return msgs


# ── Config sync check (repo behind remote) ───────────────────────────────────


def config_sync_check() -> list[str]:
    """Check if the optional memory-backend repo is behind its git remote.

    Gated behind BRAIN_ENABLED — no-op when the optional brain is disabled.
    """
    msgs: list[str] = []
    if not BRAIN_ENABLED:
        return msgs
    if not (MEMORY_REPO_DIR / ".git").exists():
        return msgs
    try:
        subprocess.run(
            ["git", "-C", str(MEMORY_REPO_DIR), "fetch", "--quiet"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        result = subprocess.run(
            [
                "git",
                "-C",
                str(MEMORY_REPO_DIR),
                "rev-list",
                "--count",
                "HEAD..@{u}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        behind = result.stdout.strip()
        if behind and behind != "0":
            msgs.append(
                f"⚠️ memory-backend repo is {behind} commit(s) behind its remote. Sync it."
            )
    except Exception:
        pass
    return msgs


# ── Dedup warning (dot-vs-hyphen encoding sibling) ───────────────────────────


def dedup_warning() -> list[str]:
    """Warn if current project dir has a dot-vs-hyphen sibling."""
    msgs: list[str] = []
    try:
        cwd = Path(os.getcwd())
        encoded = str(cwd).replace("/", "-")
        projects_dir = CLAUDE_DIR / "projects"
        if not projects_dir.is_dir():
            return msgs
        normalized = encoded.replace(".", "-")
        for d in projects_dir.iterdir():
            if d.name != encoded and d.name.replace(".", "-") == normalized:
                msgs.append(
                    f"⚠️ Duplicate project dir detected: {d.name} vs {encoded}."
                )
                break
    except Exception:
        pass
    return msgs


# ── Brain connectivity check (optional memory backend) ───────────────────────


BRAIN_CHECK_TIMEOUT_S = 10


def _run_in_own_group(
    cmd: list[str], timeout: float
) -> subprocess.CompletedProcess[str]:
    """subprocess.run, but a timeout kills the command's whole process group.

    `make` hands its pipes to the recipe's children (a node process for the
    backend's status target). subprocess.run kills only `make` on timeout, then
    waits for EOF on pipes those children still hold, so a hung child stretched
    the 10s budget to however long it lived. Raises TimeoutExpired like run().

    Process groups are POSIX. Where os.killpg does not exist (Windows) it falls
    back to killing the direct child only, as subprocess.run would. A child
    that left the group (setsid) can still hold the pipes, so the drain after
    the kill is bounded too.
    """
    with subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                if hasattr(os, "killpg"):
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
            except ProcessLookupError:
                pass  # the group exited before the kill
            except PermissionError:
                proc.kill()  # a member we may not signal: at least stop the child
            try:
                proc.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                pass  # a setsid'd descendant still holds the pipes; give up
            raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def brain_check() -> list[str]:
    """Verify the optional memory backend is reachable via its `make` target.

    Gated behind BRAIN_ENABLED — no-op when the optional brain is disabled.
    # optional: wire your own memory backend here (see docs/WORKFLOW.md)
    """
    msgs: list[str] = []
    if not BRAIN_ENABLED:
        return msgs

    makefile = MEMORY_REPO_DIR / "Makefile"
    if not makefile.exists():
        return msgs

    try:
        result = _run_in_own_group(
            ["make", "-C", str(MEMORY_REPO_DIR), "status-remote"],
            BRAIN_CHECK_TIMEOUT_S,
        )
        if result.returncode == 0 and "tier" in result.stdout.lower():
            # Parse tier counts from table output
            lines = result.stdout.strip().splitlines()
            counts = [
                l
                for l in lines
                if "│" in l and "index" not in l and "tier" not in l.lower()
            ]
            summary = ", ".join(
                c.split("│")[2].strip() + ": " + c.split("│")[3].strip()
                for c in counts
                if len(c.split("│")) >= 4
            ).replace("'", "")
            msgs.append(f"🧠 Brain: ✅ memory backend connected ({summary})")
        else:
            err = result.stderr.strip()[:100] if result.stderr else "unknown error"
            msgs.append(f"🧠 Brain: ❌ memory backend unreachable — {err}")
    except subprocess.TimeoutExpired:
        msgs.append(
            f"🧠 Brain: ❌ memory backend check timed out ({BRAIN_CHECK_TIMEOUT_S}s)"
        )
    except Exception as e:
        # Type only: exception strings can embed argv, and argv has carried a
        # connection string before.
        msgs.append(f"🧠 Brain: ⚠️ check failed — {type(e).__name__}")

    return msgs


# ── Stale memory detection ────────────────────────────────────────────────────

STALE_PROJECT_DAYS = 90  # type:project memories older than this are flagged for review


def stale_project_memories() -> list[str]:
    """Return list of `filename — N days old` strings for stale project memories.

    Scans each `<CLAUDE_DIR>/projects/*/memory/` dir for files of
    `type: project` whose mtime is older than STALE_PROJECT_DAYS. The session brief
    surfaces these so old project context is reviewed (renamed, archived, or
    refreshed) instead of silently rotting.
    """
    projects_dir = CLAUDE_DIR / "projects"
    if not projects_dir.is_dir():
        return []
    memory_dirs = [
        p / "memory" for p in projects_dir.iterdir() if (p / "memory").is_dir()
    ]
    if not memory_dirs:
        return []
    cutoff = time.time() - (STALE_PROJECT_DAYS * 86400)
    stale: list[tuple[str, int]] = []
    for memory_dir in memory_dirs:
        for f in memory_dir.glob("project_*.md"):
            try:
                if f.stat().st_mtime >= cutoff:
                    continue
                # Confirm type:project in frontmatter — skip if mistype
                head = f.read_text().split("---", 2)
                if len(head) >= 3 and "type: project" in head[1]:
                    age_days = int((time.time() - f.stat().st_mtime) / 86400)
                    stale.append((f.name, age_days))
            except Exception:
                continue
    stale.sort(key=lambda x: -x[1])  # oldest first
    return [f"{name} — {days}d old" for name, days in stale[:10]]


# ── Orphan memory drift (optional memory backend) ────────────────────────────

ThermalFetch = Callable[..., dict[str, tuple[str, float]] | None]

ORPHAN_TIMEOUT_S = 5  # matches PENDING_TIMEOUT_S — session start must not stall

# At or below this many ids, a 100%-unresolved result is unremarkable (a small
# or new project dir). Above it, "every single id is dead" far more likely means
# the wrong database than genuine total drift.
MIN_IDS_FOR_DRIFT_ALARM = 5

# A pg_id must be a real uuid before it goes to the backend. memory_dedup's own
# filter is looser, and one id that passes it but fails Postgres's ::uuid cast
# fails the whole batch — silencing the dead count for every dir.
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _load_memory_dedup():
    """Import the backend's scripts/memory_dedup.py, or None if unavailable.

    Reused rather than re-implemented: the frontmatter reader and the batched
    `pg_id -> (tier, temperature)` lookup are one contract with the backend's
    own index emitter, and a copy here would drift from it.

    Importing is safe because memory_dedup keeps every mutation behind its
    `__name__`-guarded main(). Module level is not fully inert — it evaluates
    `APPLY = "--apply" in sys.argv` — so the invariant to preserve is
    "mutations are main()-only", not "importing does nothing". The import runs
    the checkout's code at every session start, which is why it is behind
    BRAIN_ENABLED.

    This hook appends scripts/ to sys.path rather than prepending it, so the
    lookup for memory_dedup itself cannot outrank the stdlib. That is not a
    full shadowing guarantee: memory_dedup prepends its own directory when it
    is imported.
    """
    scripts_dir = MEMORY_REPO_DIR / "scripts"
    if not (scripts_dir / "memory_dedup.py").is_file():
        return None
    try:
        if str(scripts_dir) not in sys.path:
            sys.path.append(str(scripts_dir))
        import memory_dedup

        return memory_dedup
    except Exception as exc:
        print(f"[session_start] memory_dedup import failed: {type(exc).__name__}", file=sys.stderr)
        return None


def orphan_drift(
    projects_root: Path | None = None, fetch: ThermalFetch | None = None
) -> list[str]:
    """Report memory files on THIS machine whose pg_id has no live entity.

    Gated behind BRAIN_ENABLED — no-op when the optional brain is disabled.

    Orphans rank at temperature 0 in a Postgres-ranked memory index, so they
    are first in line for eviction from it, and nothing else reports them.
    Scans every project dir on the machine in one batched query: the markdown
    mirror is per-machine state, and drift shows up in dirs you are not in.

    An orphan includes a file with NO pg_id at all (memory_dedup's definition).
    Three causes are counted apart: a dead pointer needs triage, an invalid
    (non-uuid) pg_id needs repair, a missing id needs a backfill. Emits nothing
    at zero. When the DB is unreachable, or no connection string resolves,
    only the `dead` count is suppressed; `invalid` and `missing` are filesystem
    state and stay reportable, because a false "0 orphans" would be worse.
    The wrong-datasource alarm likewise suppresses only `dead`: it is followed
    by a drift line for any invalid or missing ids.
    """
    if not BRAIN_ENABLED:
        return []
    try:
        root = Path(projects_root) if projects_root else CLAUDE_DIR / "projects"
        md = _load_memory_dedup()
        fetch = fetch or (getattr(md, "fetch_thermal_by_pgid", None) if md else None)
        if md is None or fetch is None or not root.is_dir():
            return []

        files = list(_iter_memory_pgids(root, md.read_fm))
        missing: list[str] = []
        invalid: list[str] = []
        with_ids: list[tuple[str, str]] = []
        for pgid, d in files:
            if not pgid:
                missing.append(d)
            elif _UUID_RE.fullmatch(pgid):
                with_ids.append((pgid, d))
            else:
                invalid.append(d)

        resolved = _fetch_resolved(fetch, [pgid for pgid, _ in with_ids])
        # None = UNKNOWN (DB unreachable, or no connection string).
        dead = [d for pgid, d in with_ids if pgid not in resolved] if resolved is not None else []

        # A machine pointed at the wrong database answers successfully with {},
        # so every live id reads as dead. Say that instead of a drift count.
        # The filesystem causes stay reportable even then.
        if with_ids and len(dead) == len(with_ids) and len(with_ids) > MIN_IDS_FOR_DRIFT_ALARM:
            msgs = [
                f"⚠️ All {len(with_ids)} memory pg_ids unresolved against the backend — "
                "suspected wrong datasource (check DATABASE_URL / DOTENV_CONFIG_PATH), "
                "not a drift report."
            ]
            dead = []
        else:
            msgs = []

        if invalid or missing or dead:
            dirs = len(set(dead + invalid + missing))
            msgs.append(
                _drift_line(dead=len(dead), invalid=len(invalid), missing=len(missing), dirs=dirs)
            )
        return msgs
    except Exception as exc:
        # Silent in the brief, never silent everywhere.
        print(f"[session_start] orphan_drift failed: {type(exc).__name__}", file=sys.stderr)
        return []


def _fetch_resolved(
    fetch: ThermalFetch, ids: list[str]
) -> dict[str, tuple[str, float]] | None:
    """Call `fetch`, passing this hook's DB URL and timeout if it accepts them.

    Capability is read from the signature, NOT by calling and catching
    TypeError: a TypeError from deep inside a correct fetch would otherwise
    trigger a bare retry against memory_dedup's own resolver, which reads
    different files and may query a different database.

    For the same reason, the lookup is skipped and the result is UNKNOWN (None),
    never handed to memory_dedup's resolver, when the fetch's signature cannot
    be read or when it takes `url` but this hook resolves no connection string.
    """
    if not ids:
        return {}
    try:
        params = inspect.signature(fetch).parameters
    except (TypeError, ValueError):
        _log_dead_unchecked("fetch signature unreadable")
        return None
    accepts_any = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    kwargs: dict[str, object] = {}
    if accepts_any or "timeout" in params:
        kwargs["timeout"] = ORPHAN_TIMEOUT_S
    if accepts_any or "url" in params:
        url = resolve_connection_string(MEMORY_REPO_DIR)
        if url is None:
            _log_dead_unchecked("no connection string resolved")
            return None
        kwargs["url"] = url
    return fetch(ids, **kwargs)


def _log_dead_unchecked(reason: str) -> None:
    print(f"[session_start] orphan check: {reason}; dead pg_ids not checked", file=sys.stderr)


def _iter_memory_pgids(
    root: Path, read_fm: Callable[[Path], dict]
) -> Iterator[tuple[str, str]]:
    """Yield (pg_id or "", project_dir_name) for every memory file under root.

    MEMORY.md is the index, not a memory, and is never counted. An unreadable
    file is skipped rather than allowed to break session start.
    """
    for f in sorted(root.glob("*/memory/*.md")):
        if f.name == "MEMORY.md":
            continue
        try:
            yield read_fm(f)["pgid"] or "", f.parent.parent.name
        except Exception:
            continue


def _drift_line(*, dead: int, invalid: int, missing: int, dirs: int) -> str:
    """One line naming each non-zero cause. Keyword-only: bare ints are
    silently transposable otherwise."""
    parts = []
    if dead:
        parts.append(f"{dead} dead pg_id")
    if invalid:
        parts.append(f"{invalid} invalid pg_id")
    if missing:
        parts.append(f"{missing} without pg_id")
    return (
        f"⚠️ Orphan memory files: {', '.join(parts)} across {dirs} dir(s) — "
        "all rank at temp 0. The backend's `scripts/memory_dedup.py` triages "
        "the primary dir only."
    )


# ── Open specs ────────────────────────────────────────────────────────────────


def _open_specs_summary() -> str:
    """Run spec_audit.py --json, return a compact summary of open specs.

    Returns empty string if no open specs, script missing, or any error.
    Budget: max 3 lines in the brief.
    """
    if not SPEC_AUDIT.exists():
        return ""
    try:
        result = subprocess.run(
            [sys.executable, str(SPEC_AUDIT), "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return ""
        specs = json.loads(result.stdout)
        if not specs:
            return ""
        lines = [f"{s['date']}  {s['tier']:6s}  {s['stem']}" for s in specs[:5]]
        summary = "\n".join(f"- {l}" for l in lines)
        if len(specs) > 5:
            summary += f"\n- ... and {len(specs) - 5} more"
        return summary
    except Exception:
        return ""


def _pending_from_file() -> str | None:
    """Fallback: a plain-markdown pending.md, if one exists.

    Strips the file's own single-`#` preamble lines but keeps `##` entry
    headers — the documented per-entry format is `## [YYYY-MM-DD] <title>`,
    so a bare `startswith("#")` filter would silently drop every entry's
    title and leave only orphaned body text.
    """
    if not PENDING_FILE.exists():
        return None
    content = PENDING_FILE.read_text().strip()
    lines = [
        l for l in content.splitlines() if not l.startswith("#") or l.startswith("##")
    ]
    content = "\n".join(lines).strip()
    return f"## Pending\n{content}\n" if content else None


def _render_pending(stdout: str) -> str:
    """Render the host-scoped brief from the two-statement psql output.

    Line 1 is the counts row (`here_open<TAB>hidden_by_host`); the rest are
    items (`priority<TAB>title`). maxsplit=1 folds any residual tab in a title
    into the title instead of shifting the priority column.
    """
    rows = [r for r in stdout.strip().split("\n") if r.strip()]
    if not rows:
        return "## Pending\nNone.\n"

    # Trust the counts row only if it is two integers, and drop it only then:
    # consuming it unconditionally would lose the first — highest-priority —
    # item whenever it is not there. Less output beats a confident wrong one.
    counts = [c.strip() for c in rows[0].split("\t")]
    here = hidden = None
    if len(counts) == 2 and all(c.isdigit() for c in counts):
        here, hidden = counts
        item_rows = rows[1:]
    else:
        item_rows = rows
        print(
            f"[session_start] pending: unparseable counts row {rows[0][:80]!r}; "
            "rendering all rows as items, count omitted",
            file=sys.stderr,
        )

    lines = [
        f"- [{p[0]}] {p[1]}" for p in (r.split("\t", 1) for r in item_rows) if len(p) == 2
    ]

    # Reported even when it is the only thing to report: an item scoped to a
    # hostname that does not exist is invisible on every machine, and without
    # this number that looks exactly like it was resolved.
    scope_note = f" · {hidden} scoped to other hosts" if hidden not in (None, "0") else ""
    if not lines:
        return f"## Pending ({here or 0} open here{scope_note})\nNone for this machine.\n"
    shown = f"{here} open here, showing {len(lines)}" if here else f"showing {len(lines)}"
    return f"## Pending ({shown}{scope_note})\n" + "\n".join(lines) + "\n"


def _render_pending_legacy(stdout: str) -> str:
    """Render the unscoped query, for a backend without applies_to."""
    rows = [r for r in stdout.strip().split("\n") if r.strip()]
    if not rows:
        return "## Pending\nNone.\n"

    # total_open is column 0 — positionally safe even if a title (column 2,
    # the last field) still carries a stray tab: a bounded split(maxsplit=2)
    # folds any residual tabs into the title text instead of shifting the
    # earlier columns.
    total = rows[0].split("\t", 2)[0]
    lines = []
    for row in rows:
        parts = row.split("\t", 2)
        if len(parts) >= 3:
            lines.append(f"- [{parts[1]}] {parts[2]}")

    header = f"## Pending ({total} open, showing {len(lines)})"
    return header + "\n" + "\n".join(lines) + "\n"


def _psql_env(conn: str) -> dict[str, str] | None:
    """Translate a Postgres URL into PG* environment variables.

    The connection string is deliberately NOT passed as an argv element:
    argv is world-readable via `ps`, and — more sharply — it is embedded in
    `subprocess.TimeoutExpired`'s string form, so a routine 5-second timeout
    would print the password into the hook's stderr. Returns None if the URL
    can't be parsed, so the caller falls back rather than guessing.
    """
    parsed = urllib.parse.urlsplit(conn)
    if not parsed.hostname:
        return None

    # Drop every inherited PG* variable before setting our own. Two reasons:
    # an ambient PGHOSTADDR outranks PGHOST and would silently point psql at a
    # different server than the URL names, and a stale PGDATABASE/PGUSER would
    # fill in any component this URL happens to omit. PGSERVICE and PGPASSFILE
    # can likewise redirect the whole connection.
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}

    env["PGHOST"] = parsed.hostname
    if parsed.port:
        env["PGPORT"] = str(parsed.port)
    if parsed.username:
        env["PGUSER"] = urllib.parse.unquote(parsed.username)
    if parsed.password:
        env["PGPASSWORD"] = urllib.parse.unquote(parsed.password)
    database = parsed.path.lstrip("/")
    if database:
        env["PGDATABASE"] = urllib.parse.unquote(database)
    sslmode = urllib.parse.parse_qs(parsed.query).get("sslmode")
    if sslmode:
        env["PGSSLMODE"] = sslmode[0]
    return env


def _run_psql(env: dict[str, str], *statements: str) -> subprocess.CompletedProcess:
    """Run statements in ONE psql connection, tab-separated, tuples only.

    ON_ERROR_STOP is load-bearing with several -c statements: psql reports only
    the LAST one's status, so a failing counts query beside a succeeding items
    query would otherwise exit 0. VERBOSITY=verbose puts the SQLSTATE in stderr.
    -X skips ~/.psqlrc, whose settings could add lines to stdout.
    """
    argv = ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-v", "VERBOSITY=verbose", "-t", "-A", "-F", "\t"]
    for sql in statements:
        argv += ["-c", sql]
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=PENDING_TIMEOUT_S, env=env
    )


def _is_missing_applies_to(stderr: str | None) -> bool:
    """True only for undefined_column naming applies_to — a backend that
    predates host scoping. Recognised by SQLSTATE 42703 (VERBOSITY=verbose) or
    by Postgres's own `column "applies_to" does not exist` wording, so it holds
    whichever form psql prints. Any other error naming the column (a permission
    error, say) is a real failure and must not trigger the unscoped retry.
    See PENDING_LEGACY_SQL for when this fallback expires.
    """
    if not stderr or "applies_to" not in stderr:
        return False
    return "42703" in stderr or 'column "applies_to" does not exist' in stderr


def _pending_from_db() -> str | None:
    """Query the optional backend's `pending` table. None if unavailable.

    Never raises: every failure mode (no backend configured, no psql on PATH,
    unparseable URL, unreachable database, query timeout) returns None so the
    caller can fall back to the file tier.
    """
    if not BRAIN_ENABLED:
        return None
    try:
        conn = resolve_connection_string(MEMORY_REPO_DIR)
        if not conn:
            return None
        if not shutil.which("psql"):
            print("[session_start] pending: psql not on PATH", file=sys.stderr)
            return None
        env = _psql_env(conn)
        if env is None:
            print("[session_start] pending: unparseable connection string", file=sys.stderr)
            return None

        result = _run_psql(env, _counts_sql(), _items_sql())
        render = _render_pending
        if result.returncode != 0 and _is_missing_applies_to(result.stderr):
            print(
                "[session_start] pending: backend has no applies_to column; "
                "using the unscoped query (apply the backend's migrations)",
                file=sys.stderr,
            )
            result = _run_psql(env, PENDING_LEGACY_SQL)
            render = _render_pending_legacy
        if result.returncode != 0:
            # stderr can echo the target host but never the password (it is
            # passed via PGPASSWORD), so it is safe — and useful — to surface.
            print(
                f"[session_start] pending query exited {result.returncode}: "
                f"{result.stderr.strip()[:200]}",
                file=sys.stderr,
            )
            return None
        return render(result.stdout)
    except Exception as e:
        # Log the exception TYPE only. Several exception strings (notably
        # subprocess.TimeoutExpired) embed the full argv, and a future edit
        # that puts a credential back on the command line must not silently
        # turn this line into a password leak.
        print(f"[session_start] pending query failed: {type(e).__name__}", file=sys.stderr)
        return None


def pending_section() -> str:
    """Render the pending brief: titles only, priority-ordered, capped.

    Two sources, in order, so the hook works with or without a backend:

      1. the `pending` table in the optional memory backend (BRAIN_ENABLED),
         via `pending_add` / `pending_list` / `pending_resolve`
      2. a plain `pending.md` file — the zero-setup default

    Fail-open at every step: this must NEVER raise or block session start.
    """
    from_db = _pending_from_db()
    if from_db is not None:
        return from_db

    try:
        fallback = _pending_from_file()
    except Exception as e:
        print(f"[session_start] pending file read failed: {e}", file=sys.stderr)
        fallback = None

    return fallback or "## Pending\nNone.\n"


# ── Concurrent DB-bound checks ───────────────────────────────────────────────

# The pending brief when pending_section itself RAISES (it is fail-open, so
# this is only a backstop). Not "None." so that an unexpected crash is not
# mistaken for an empty queue. pending_section's own handled failures (DB down,
# no pending.md) still render "None.", by the zero-setup design.
PENDING_UNAVAILABLE = "## Pending\nunavailable (check failed)\n"


class DbChecks(NamedTuple):
    """Results of _run_db_checks. Named, because brain and orphans have the
    same type and a positional swap would type-check and route each one into
    the other's output group."""

    brain: list[str]
    orphans: list[str]
    pending: str


def _run_db_checks() -> DbChecks:
    """Run brain_check, orphan_drift and pending_section concurrently.

    Each waits on its own subprocess timeout, so in sequence an unreachable
    backend cost the sum of the three and concurrently it costs the slowest.
    None of them writes state another reads. One caveat: orphan_drift's import
    of the backend's memory_dedup extends sys.path while the other threads may
    be importing; the import lock serialises the imports themselves, and the
    same extension already happened before pending_section when the checks ran
    in sequence. With BRAIN_ENABLED off, the
    first two return at once and only the pending.md read remains.

    Each check already fails open; the per-future guard is a backstop so one
    check that raises can never take the other two with it. It logs the
    exception TYPE only: several exception strings (TimeoutExpired among them)
    embed argv, and argv has carried a connection string before.

    Must run after memory_decay, which can rewrite the memory files that
    orphan_drift reads.
    """
    checks = (
        ("brain", brain_check),
        ("orphans", orphan_drift),
        ("pending", pending_section),
    )
    with ThreadPoolExecutor(max_workers=len(checks)) as pool:
        futures = [(name, pool.submit(fn)) for name, fn in checks]
    results: dict[str, list[str] | str] = {}
    for name, future in futures:
        try:
            results[name] = future.result()
        except Exception as exc:
            print(
                f"[session_start] {name} check failed: {type(exc).__name__}",
                file=sys.stderr,
            )
            results[name] = _db_check_fallback(name, exc)
    return DbChecks(
        brain=cast(list[str], results["brain"]),
        orphans=cast(list[str], results["orphans"]),
        pending=cast(str, results["pending"]),
    )


def _db_check_fallback(name: str, exc: Exception) -> list[str] | str:
    if name == "brain":
        return [f"🧠 Brain: ⚠️ check failed — {type(exc).__name__}"]
    if name == "orphans":
        return []
    return PENDING_UNAVAILABLE


# ── Brief builder ─────────────────────────────────────────────────────────────


def build_brief(state: dict, resumed: bool, pending: str | None = None) -> str:
    """Build the session brief. `pending` is the already-rendered pending
    section from _run_db_checks; when None it is queried here."""
    now = datetime.now()
    sections = [f"# Session Brief — {now.strftime('%Y-%m-%d %H:%M')}\n"]

    # Last session gap
    last = state.get("last_active", "")
    msg_count = state.get("user_message_count", 0)
    wrap_ran = state.get("wrap_up_ran", False)

    if resumed and last:
        try:
            last_dt = (
                datetime.fromisoformat(last.replace("Z", "+00:00"))
                .astimezone()
                .replace(tzinfo=None)
            )
            delta = now - last_dt
            h = int(delta.total_seconds() // 3600)
            m = int((delta.total_seconds() % 3600) // 60)
            gap = (
                f"{h // 24}d ago"
                if h > 48
                else (f"{h}h {m}m ago" if h else f"{m}m ago")
            )
        except Exception:
            gap = "unknown"
        wrap_str = "✅ completed" if wrap_ran else "⚠️  skipped"
        sections.append(
            f"## Resumed Session\n- Last active: {gap}\n- Messages: {msg_count}\n- Wrap-up: {wrap_str}\n"
        )
    else:
        sections.append("## New Session\n")

    # Working directory
    sections.append(f"## Machine\n`{MACHINE_ID}` ({sys.platform})\n")
    sections.append(f"## Working Directory\n`{os.getcwd()}`\n")

    # Git
    git_items = git_summary()
    sections.append("## Git\n" + "\n".join(git_items) + "\n")

    # Pending items
    sections.append(pending if pending is not None else pending_section())

    # Recent learnings (last 3 entries)
    if LEARNINGS.exists():
        raw = LEARNINGS.read_text().strip()
        entries = [
            e.strip() for e in raw.split("\n## ") if e.strip() and not e.startswith("#")
        ]
        if entries:
            recent = entries[-3:]
            formatted = "\n\n".join("## " + e for e in recent)
            sections.append(f"## Recent Learnings\n{formatted}\n")

    # Open specs (from spec_audit.py — only shown if >0 open)
    open_specs = _open_specs_summary()
    if open_specs:
        sections.append(f"## Open Specs\n{open_specs}\n")

    # Stale project memories (older than 90 days — needs review or archive)
    stale = stale_project_memories()
    if stale:
        bullet_list = "\n".join(f"- {s}" for s in stale)
        sections.append(
            f"## ⚠️  Stale Project Memories ({STALE_PROJECT_DAYS}+ days old)\n"
            f"Review these — refresh, archive, or delete:\n{bullet_list}\n"
        )

    return "\n---\n".join(sections)


# ── Config-checkout checks ────────────────────────────────────────────────────


def _managed_checkout(hooks_link: Path | None = None) -> tuple[Path, str] | None:
    """(toplevel, branch) of the config checkout that ~/.claude/hooks links into.

    None unless CRAFT_CONFIG_REPO is set, when the hooks are real-file copies,
    when the link does not lead into a git checkout, when that checkout is not
    CRAFT_CONFIG_REPO, or when the link is not `<toplevel>/<CRAFT_CONFIG_SUBDIR>/hooks`.
    """
    if CONFIG_REPO is None:
        return None  # silent until configured
    link = hooks_link or CLAUDE_DIR / "hooks"
    if not link.is_symlink():
        return None
    try:
        target = link.resolve()
        res = subprocess.run(
            [
                "git", "-c", "core.fsmonitor=false", "-C", str(target),
                "rev-parse", "--show-toplevel", "--abbrev-ref", "HEAD",
            ],
            capture_output=True,
            text=True,
            errors="replace",  # a non-UTF-8 path must not raise out of the brief
            timeout=5,
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired):  # RuntimeError: link loop
        return None
    lines = res.stdout.splitlines()  # not split(): a repo path may hold spaces
    if res.returncode != 0 or len(lines) != 2:
        return None  # not a git checkout: nothing to say
    toplevel, branch = lines
    # Only the managed layout: git walks up from any directory, and ~/.claude
    # is itself a repo, so a link elsewhere could report an unrelated branch.
    if target != _config_dir(Path(toplevel)) / "hooks":
        return None
    try:
        if Path(toplevel).resolve() != CONFIG_REPO.resolve():
            return None  # some other checkout: not the one you asked about
    except (OSError, RuntimeError):
        return None
    return Path(toplevel), branch


def live_hooks_branch(hooks_link: Path | None = None) -> list[str]:
    """Warn when the live hooks come from a checkout that is not on its main branch.

    When `~/.claude/hooks` symlinks into a working clone that feature work also
    switches branches in, every session's guards are whatever branch was last
    checked out, so a merged fix can stay dead. Silent on CRAFT_CONFIG_BRANCH
    (default `main`), and silent where hooks are real-file copies.
    """
    checkout = _managed_checkout(hooks_link)
    if checkout is None:
        return []
    toplevel, branch = checkout
    if branch == CONFIG_BRANCH:
        return []
    where = "a detached HEAD" if branch == "HEAD" else f"branch {branch}"
    return [
        f"⚠️ live hooks come from {where} (not {CONFIG_BRANCH}) in {toplevel} "
        f"— merged fixes are not live until it returns to {CONFIG_BRANCH}."
    ]


CONFIG_UNTRACKED_SHOWN = 3


def config_untracked(hooks_link: Path | None = None) -> list[str]:
    """Warn when the config checkout has untracked files.

    A new skill, rule or hook dropped into the checkout is live at once but
    not in git, so it is lost with the machine and never reaches your other
    machines. Untracked only — modified tracked files are routine churn.
    Silent when clean, and where hooks are real-file copies.
    """
    checkout = _managed_checkout(hooks_link)
    if checkout is None:
        return []
    toplevel, _branch = checkout
    try:
        res = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(toplevel),
             "status", "--porcelain", "-z", "--", CONFIG_SUBDIR or "."],
            # errors="replace": a non-UTF-8 name must not raise out of the brief
            capture_output=True, text=True, errors="replace", timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if res.returncode != 0:
        return []
    # -z turns off git's quoting, so a name arrives raw: mask control
    # characters before it reaches the brief.
    paths = [
        "".join(c if c.isprintable() else "?" for c in e[3:])
        for e in res.stdout.split("\0")
        if e.startswith("?? ")
    ]
    if not paths:
        return []
    shown = ", ".join(paths[:CONFIG_UNTRACKED_SHOWN])
    more = len(paths) - CONFIG_UNTRACKED_SHOWN
    if more > 0:
        shown += f" (+{more} more)"
    return [
        f"⚠️ {len(paths)} untracked path(s) in the config checkout {toplevel} "
        f"— live now, but not in git until you commit them: {shown}"
    ]


CANARY_SKEW_S = 300  # a stamp this far in the future is not clock jitter


def canary_liveness(
    stamp: Path | None = None, max_age_h: float | None = None, now: float | None = None
) -> list[str]:
    """Warn when a heartbeat file you configured has gone stale.

    For a scheduled job whose silence would otherwise go unnoticed (a watcher,
    a backup): have it write the current Unix time to CRAFT_CANARY_FILE on
    every run, and set CRAFT_CANARY_MAX_AGE_H to a few missed intervals. A
    stale or missing stamp means the job is not running at all. Silent unless
    CRAFT_CANARY_FILE is set.
    """
    stamp = stamp if stamp is not None else CANARY_FILE
    max_age_h = max_age_h if max_age_h is not None else CANARY_MAX_AGE_H
    if stamp is None:
        return []
    now = time.time() if now is None else now
    try:
        # float first: a job writing Python's time.time() leaves a decimal stamp.
        last = float(stamp.read_text().strip())
        if not math.isfinite(last) or not 0 <= last < 1e11:
            raise ValueError("not a plausible Unix time")
    except (OSError, ValueError):
        return [f"⚠️ canary: no run recorded at {stamp} — the job may never have run."]
    age = now - last
    if age < -CANARY_SKEW_S:
        # A future stamp would otherwise read as healthy forever.
        return [f"⚠️ canary stamp at {stamp} is in the future — cannot tell if the job runs."]
    if age <= max_age_h * 3600:
        return []
    return [f"⚠️ canary has not run for {age / 3600:.0f}h (limit {max_age_h:g}h) — check the job behind {stamp}."]


GITLEAKS_MIN = (8, 19)  # `gitleaks git`, which the pre-commit hook runs
MIN_TEXT = ".".join(map(str, GITLEAKS_MIN))


def secret_scan_status(
    home: Path | None = None, env: dict[str, str] | None = None
) -> list[str]:
    """Warn when a global pre-commit secret scan you rely on is not really running.

    A global git hook that runs gitleaks typically fails OPEN without the
    binary (one stderr line per commit, easy to miss) and CLOSED on a gitleaks
    too old for `gitleaks git`, so a machine can sit unscanned or stuck for
    weeks. Checks that the global core.hooksPath points at CRAFT_GIT_HOOKS_DIR,
    then finds gitleaks on PATH or GITLEAKS_HOOK_SEARCH_PATH. Silent unless
    CRAFT_GIT_HOOKS_DIR is set.
    """
    home = home or Path.home()
    env = env if env is not None else dict(os.environ)
    raw = env.get("CRAFT_GIT_HOOKS_DIR", "")
    if not raw:
        return []
    ours = Path(str(home) + raw[1:]) if raw == "~" or raw.startswith("~/") else Path(raw)
    if ours.is_symlink() and not ours.exists():
        # The clone it points into is on a branch without the hooks dir:
        # git finds no hooks there and runs none, silently.
        return [
            f"⚠️ commits are NOT scanned for secrets: {ours} points to the missing "
            f"{os.readlink(ours)} — switch that clone back to {CONFIG_BRANCH}."
        ]
    if not ours.exists():
        return []
    msgs: list[str] = []
    try:
        res = subprocess.run(
            ["git", "config", "--global", "--get", "core.hooksPath"],
            capture_output=True, text=True, errors="replace", timeout=5, env=env,
        )
        current = res.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        current = ""
    if current == "~" or current.startswith("~/"):
        current_path = str(home) + current[1:]
    else:
        current_path = current
    configured = Path(current_path) if current else None
    try:
        same = configured is not None and configured.resolve() == ours.resolve()
    except (OSError, RuntimeError):  # RuntimeError: a symlink loop
        same = False
    if configured is None:
        msgs.append(
            "⚠️ commits are NOT scanned for secrets: no global core.hooksPath — "
            f"run `git config --global core.hooksPath {ours}`."
        )
    elif not same:
        msgs.append(
            f"⚠️ commits are NOT scanned for secrets: global core.hooksPath is "
            f"{current}, not {ours}."
        )

    search = env.get(
        "GITLEAKS_HOOK_SEARCH_PATH",
        f"/opt/homebrew/bin:/usr/local/bin:{home}/.local/bin:{home}/bin",
    )
    dirs = [*env.get("PATH", "").split(":"), *search.split(":")]
    binary = next(
        # Absolute dirs only: a relative entry like `.` would run a gitleaks
        # planted in whatever project the session starts in.
        (
            Path(d) / "gitleaks"
            for d in dirs
            if d and Path(d).is_absolute() and os.access(Path(d) / "gitleaks", os.X_OK)
        ),
        None,
    )
    if binary is None:
        msgs.append(
            "⚠️ commits are NOT scanned for secrets: no gitleaks binary on PATH "
            f"or the hook's search path — install gitleaks {MIN_TEXT}+."
        )
        return msgs
    # A literal argv[0], with the found directory first on PATH, so this call
    # can only ever run `gitleaks`.
    run_env = {**env, "PATH": f"{binary.parent}:{env.get('PATH', '')}"}
    try:
        out = subprocess.run(
            ["gitleaks", "version"], capture_output=True, text=True, errors="replace",
            timeout=5, env=run_env,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return msgs
    m = re.search(r"(\d+)\.(\d+)", out)
    if m and (int(m.group(1)), int(m.group(2))) < GITLEAKS_MIN:
        msgs.append(
            f"⚠️ every commit is BLOCKED: gitleaks {m.group(0)} at {binary} is older "
            f"than {MIN_TEXT} (no `gitleaks git`) — upgrade it."
        )
    return msgs


# ── Deployed-hook drift ──────────────────────────────────────────────────────

# Deployable files live in these subdirectories of the config checkout and are
# deployed to the same-named subdirectory of ~/.claude. settings.json can add
# more destinations on top (see _hook_drift_targets).
HOOK_DRIFT_SOURCE_DIRS = ("hooks", "scripts")
HOOK_DRIFT_SUFFIXES = (".py", ".sh")
# Files you keep as deliberate per-machine forks. Extend with
# CRAFT_HOOK_DRIFT_EXCLUDE (comma-separated basenames).
HOOK_DRIFT_EXCLUDE = frozenset(
    {"settings.json"}
    | {n.strip() for n in os.environ.get("CRAFT_HOOK_DRIFT_EXCLUDE", "").split(",") if n.strip()}
)
_INVOKED_PATH_RE = re.compile(r"[~$@\w./{}-]{0,256}/[\w.-]{1,128}\.(?:py|sh)(?![\w.])")
_SETTINGS_SCAN_LIMIT = 200_000
_ENV_VAR_RE = re.compile(r"\$\{(\w+)\}|\$(\w+)")


def _expand_home(token: str, home: Path) -> Path | None:
    """Resolve ~ and $VAR / ${VAR} in a settings.json path token.

    Environment variables are expanded generally rather than special-casing
    HOME, so $CLAUDE_PROJECT_DIR — which Claude Code itself sets and which real
    settings files use — resolves instead of being silently skipped. HOME is
    overridden with the caller's value so tests can point at a fake home.

    Substitution is anchored on a whole variable name: a naive
    `replace("$HOME", ...)` also rewrites `$HOMEBREW_PREFIX`, producing a path
    under a home directory that does not exist.

    Returns None for anything still relative, and for anything that escapes
    `home` once resolved. A relative command is resolved against a working
    directory this process does not share, and `..` segments could otherwise
    walk the scan outside the deploy tree entirely. Both are silent skips
    rather than errors — a path this code cannot place is not drift.
    """
    env = {**os.environ, "HOME": str(home)}

    def sub(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        return env.get(name, match.group(0))

    text = _ENV_VAR_RE.sub(sub, token)
    if text.startswith("~/"):
        text = str(home) + text[1:]
    # normpath collapses `..` TEXTUALLY. The path is never resolve()d: doing so
    # follows a symlinked ~/.claude/hooks back into the repo, making every
    # settings-derived hook a self-pair under a third "destination" that is
    # really the source — inflating the coverage counts this alert reports, and
    # defeating dedup against the static floor.
    candidate = Path(os.path.normpath(text))
    try:
        # Containment is TEXTUAL, on the normalized path. normpath already
        # collapsed `..`, so $HOME/../../etc/hooks/x.py cannot pass — while a
        # legitimate ~/.claude/hooks that SYMLINKS outside $HOME still does.
        # Validating the resolved path instead dropped every settings-derived
        # target on such a machine, silently.
        candidate.relative_to(Path(os.path.normpath(str(home))))
    except ValueError:
        return None
    return candidate


def _settings_invoked_paths(settings_path: Path, home: Path) -> list[Path]:
    """Absolute .py/.sh paths invoked by the LIVE settings.json.

    Read as TEXT and matched with a regex rather than parsed as JSON on
    purpose: commands live at several different depths and key names, and a
    settings file that fails to parse should still yield its paths rather than
    silently contributing nothing.

    ValueError is caught alongside OSError because read_text() raises
    UnicodeDecodeError (a ValueError) on a non-UTF-8 file. Letting that escape
    reached hook_drift's outer handler and disabled the WHOLE check while this
    docstring claimed the static floor still applied.
    """
    try:
        raw = settings_path.read_text()[:_SETTINGS_SCAN_LIMIT]
    except (OSError, ValueError):
        return []  # absent, unreadable, or not text — the static floor applies
    out: list[Path] = []
    for match in _INVOKED_PATH_RE.finditer(raw):
        resolved = _expand_home(match.group(0), home)
        if resolved is not None:
            out.append(resolved)
    return out


def _hook_drift_targets(
    repo_root: Path,
    claude_dir: Path,
    settings_path: Path,
    home: Path | None = None,
) -> list[tuple[Path, Path]]:
    """Ordered, deduped (repo_file, deployed_file) pairs to compare.

    `home` anchors ~ and $HOME expansion and defaults to claude_dir.parent,
    which holds for the real layout (CLAUDE_DIR = ~/.claude).

    Two sources, unioned:

    1. The static floor — every deployable file under <config>/<sub>/ against
       ~/.claude/<sub>/. A file whose basename is unique across the source dirs
       is ALSO paired against the other deploy dirs when a copy exists there,
       since a hook can be installed into scripts/ (or the reverse). Skipped
       for an ambiguous basename, which cannot be attributed to one source.
    2. The live settings.json — every path it actually invokes whose basename
       has a counterpart in the checkout. That filter is what makes it safe to
       read a real settings file: a path into some unrelated project has no
       counterpart and is ignored rather than compared against nothing. It is
       also how a machine-specific destination is found without hardcoding it.
    """
    if home is None:
        home = claude_dir.parent
    config = _config_dir(repo_root)

    # name -> [(source subdir, repo file)]. Keyed as a LIST, not a single
    # entry: a bare basename index silently attributed a deployed file to
    # whichever source dir was scanned first, reporting a byte-identical file
    # as diverged.
    sources: dict[str, list[tuple[str, Path]]] = {}
    pairs: list[tuple[Path, Path]] = []
    seen: set[tuple[str, str]] = set()

    def add(repo_file: Path, deployed_file: Path) -> None:
        key = (str(repo_file), str(deployed_file))
        if key not in seen:
            seen.add(key)
            pairs.append((repo_file, deployed_file))

    for sub in HOOK_DRIFT_SOURCE_DIRS:
        source_dir = config / sub
        if not source_dir.is_dir():
            continue
        for repo_file in sorted(source_dir.iterdir()):
            if repo_file.suffix not in HOOK_DRIFT_SUFFIXES or not repo_file.is_file():
                continue
            sources.setdefault(repo_file.name, []).append((sub, repo_file))

    for name, candidates in sorted(sources.items()):
        for sub, repo_file in candidates:
            add(repo_file, claude_dir / sub / name)
        if len(candidates) == 1:
            _, repo_file = candidates[0]
            for other in HOOK_DRIFT_SOURCE_DIRS:
                cross = claude_dir / other / name
                # Only when it actually exists: a cross-dir install is the
                # exception, so pairing every file against every
                # deploy dir invents targets that can never be found and makes
                # the reported denominator a fiction.
                if cross.is_file():
                    add(repo_file, cross)

    for deployed_file in _settings_invoked_paths(settings_path, home):
        # TWO conditions, and both are load-bearing. Basename alone would
        # compare <config>/scripts/probe.sh against an unrelated project's
        # scripts/probe.sh and report drift that means nothing — and a check
        # the operator learns to skim is worse than no check. Requiring the
        # parent directory to be named like a deploy target bounds that to a
        # foreign repo that also uses hooks/ or scripts/ AND collides on a
        # filename, which is documented as the accepted residual risk.
        if deployed_file.parent.name not in HOOK_DRIFT_SOURCE_DIRS:
            continue
        candidates = sources.get(deployed_file.name, [])
        if len(candidates) == 1:
            add(candidates[0][1], deployed_file)
            continue
        # Ambiguous basename: attribute it by the deploy directory's name, and
        # skip entirely if that still does not single one out. Guessing here is
        # what produces a false "diverged" on a file that is in sync.
        matched = [rf for sub, rf in candidates if sub == deployed_file.parent.name]
        if len(matched) == 1:
            add(matched[0], deployed_file)

    return pairs


def hook_drift(
    deployed_dir: Path | None = None,
    repo_hooks: Path | None = None,
    *,
    repo_root: Path | None = None,
    claude_dir: Path | None = None,
    settings_path: Path | None = None,
) -> list[str]:
    """Warn when deployed hooks and scripts differ from the config checkout.

    Where ~/.claude/hooks holds real file COPIES, nothing else compares them
    with the repo, so an edited deployed copy (a local fork) can be silently
    overwritten by the next copy step, and a stale one keeps running old code.
    Each differing file is classified by direction: AHEAD (the deployed copy
    has lines the repo lacks — upstream it first), stale (safe to re-copy), or
    diverged (both). A same-file pair (a symlinked hooks dir) is skipped per
    pair, not for the whole check.

    Two calling forms: (deployed_dir, repo_hooks) compares one directory pair;
    (repo_root, claude_dir, settings_path) runs discovery, defaulting to
    CRAFT_CONFIG_REPO and this machine's ~/.claude. Silent when
    CRAFT_CONFIG_REPO is unset or a directory is missing, and fail-open: any
    error is logged to stderr and the check reports nothing.

    Returns 0 or 1 strings, matching the sibling checks in this file.
    """
    explicit = deployed_dir is not None or repo_hooks is not None
    discovery = (
        repo_root is not None or claude_dir is not None or settings_path is not None
    )
    if explicit and discovery:
        raise TypeError(
            "hook_drift: pass either (deployed_dir, repo_hooks) or "
            "(repo_root, claude_dir, settings_path), not both"
        )
    try:
        if explicit:
            deployed = (
                Path(deployed_dir) if deployed_dir is not None else CLAUDE_DIR / "hooks"
            )
            repo = (
                Path(repo_hooks)
                if repo_hooks is not None
                else (_config_dir(CONFIG_REPO) / "hooks" if CONFIG_REPO else None)
            )
            if repo is None:
                return []
            if not deployed.is_dir() or not repo.is_dir():
                return []
            pairs = [
                (f, deployed / f.name)
                for f in sorted(repo.iterdir())
                if f.suffix in HOOK_DRIFT_SUFFIXES and f.is_file()
            ]
        else:
            root = Path(repo_root) if repo_root is not None else CONFIG_REPO
            if root is None:
                return []
            live_dir = Path(claude_dir) if claude_dir is not None else CLAUDE_DIR
            settings = (
                Path(settings_path)
                if settings_path is not None
                else live_dir / "settings.json"
            )
            pairs = _hook_drift_targets(root, live_dir, settings)
            if not pairs:
                print(
                    "[session_start] hook_drift found no targets under "
                    f"{_config_dir(root)} — repo checkout not found? "
                    "This check is covering nothing.",
                    file=sys.stderr,
                )

        drift: dict[str, list[str]] = {"fork": [], "stale": [], "diverged": []}
        targets = 0
        compared = 0
        destinations: set[str] = set()

        for repo_file, live in pairs:
            name = repo_file.name
            if name in HOOK_DRIFT_EXCLUDE:
                continue
            targets += 1
            destinations.add(str(live.parent))
            if not live.is_file():
                continue  # not deployed on this machine — not drift
            try:
                # PER-PAIR symlink/hardlink skip. Same file on disk (a
                # symlinked hooks dir, or a hardlinked script) means
                # there is nothing to compare — but only for THIS pair.
                if repo_file.resolve() == live.resolve() or repo_file.samefile(live):
                    continue
            except OSError as exc:
                print(
                    f"[session_start] hook_drift could not stat {name}: {exc}",
                    file=sys.stderr,
                )
                continue
            try:
                a = repo_file.read_text().splitlines()
                b = live.read_text().splitlines()
            except Exception as exc:  # noqa: S112 - logged, not swallowed
                # An unreadable hook is itself worth knowing about, but it must
                # not stop the other files from being checked.
                print(
                    f"[session_start] hook_drift skipped {name}: {exc}",
                    file=sys.stderr,
                )
                continue
            compared += 1
            if a == b:
                continue
            # Set comparison, not a diff: we only need the DIRECTION.
            only_repo, only_live = set(a) - set(b), set(b) - set(a)
            if only_live and only_repo:
                drift["diverged"].append(name)
            elif only_live:
                drift["fork"].append(name)  # deployed has content the repo lacks
            elif only_repo:
                drift["stale"].append(name)
            else:
                # Neither side has a unique line, yet the files differ — a pure
                # reordering, reindent, or a duplicated line. Deliberately
                # SILENT: this check exists to stop a destructive copy and to
                # surface unreviewed local code, and this is neither. Nothing
                # unique is lost by overwriting, so there is nothing to act on.
                #
                # The accepted blind spot is a file whose only change is a
                # duplicated existing line. Reporting every reindent to catch
                # that would trade a real false-positive stream for a
                # near-impossible one, and a check the operator learns to skim
                # is worse than no check.
                continue

        if not any(drift.values()):
            return []
        return [_hook_drift_line(drift, compared, targets, len(destinations))]
    except Exception as exc:
        # Silent in the brief, but never silent everywhere — otherwise a real
        # failure is indistinguishable from "no drift".
        print(f"[session_start] hook_drift failed: {exc}", file=sys.stderr)
        return []


def _hook_drift_line(
    drift: dict[str, list[str]], compared: int, targets: int, destinations: int
) -> str:
    """One line, forks named first — they are the only destructive case.

    Stale files are counted, not named: they are fixed by re-copying. Forks and
    diverged files are named, because each needs a human to look at that file
    before anything is copied over it. The coverage clause says how much was
    actually compared AND how much was discovered, so a check covering part of
    the surface does not read as covering all of it.
    """
    parts = []
    if drift["fork"]:
        parts.append(f"{len(drift['fork'])} AHEAD of repo ({', '.join(drift['fork'])})")
    if drift["diverged"]:
        names = ", ".join(drift["diverged"])
        parts.append(f"{len(drift['diverged'])} diverged ({names})")
    if drift["stale"]:
        parts.append(f"{len(drift['stale'])} stale")
    needs_review = drift["fork"] or drift["diverged"]
    verb = "upstream before copying" if needs_review else "safe to re-copy"
    return (
        f"⚠️ Deployed hooks differ from the repo: {', '.join(parts)} — {verb}. "
        f"Scanned {compared} of {targets} target(s) across {destinations} "
        "destination(s); copies installed by launchd/systemd units are not covered."
    )


# ── Stdout budget ───────────────────────────────────────────────────────────


@dataclass
class MsgGroup:
    """One source of stdout lines. `rank` None means an alert group, which
    never degrades; a degradable group shrinks to a pointer, lowest rank first."""

    name: str
    lines: list[str]
    rank: int | None = None


def build_groups(
    *,
    sync: list[str],
    decay: list[str],
    evolve: list[str],
    config: list[str],
    dedup: list[str],
    brain: list[str],
    orphans: list[str],
    hook_drift: list[str],
) -> list[MsgGroup]:
    """main()'s groups, in output order. Correctness alerts (config, brain,
    orphans, hook_drift) have no rank and never degrade; the rest degrade
    cheapest-first: evolve nudges, then decay, sync, dedup."""
    return [
        MsgGroup("sync", sync, rank=2),
        MsgGroup("decay", decay, rank=1),
        MsgGroup("evolve", evolve, rank=0),
        MsgGroup("config", config),
        MsgGroup("dedup", dedup, rank=3),
        MsgGroup("brain", brain),
        MsgGroup("orphans", orphans),
        MsgGroup("hook_drift", hook_drift),
    ]


def _stdout_footer() -> str:
    return (
        f"Read {BRIEF_FILE} and present a session orientation (max 10 lines).\n"
        "Lead with the most actionable item: pending items > dirty repos > last session gap.\n"
        "If everything is clean, say so briefly. Tone: oriented co-pilot, not a greeter."
    )


def _clip(line: str) -> str:
    return line if len(line) <= _LINE_CAP else line[:_LINE_CAP] + " …(clipped)"


def _write_overflow(overflow_file: Path, text: str) -> bool:
    """Best effort. A failure here must never cost the session its stdout."""
    try:
        overflow_file.parent.mkdir(parents=True, exist_ok=True)
        overflow_file.write_text(text)
        return True
    except OSError as e:
        print(f"[session_start] overflow write failed: {e}", file=sys.stderr)
        return False


def _clear_overflow(overflow_file: Path) -> None:
    """A previous run's overflow must not outlive the session it described."""
    try:
        overflow_file.unlink(missing_ok=True)
    except OSError:
        pass


def _degrade(groups: list[MsgGroup], current: dict[str, list[str]], fits, where: str) -> list[str]:
    """Shrink degradable groups to a pointer, cheapest rank first, until the
    body fits — and only where the pointer is shorter than what it replaces.
    Mutates `current`; returns the names of the groups it shrank."""
    dropped: list[str] = []
    degradable = sorted(
        (group for group in groups if group.rank is not None and group.lines),
        key=lambda group: group.rank,
    )
    for group in degradable:
        if fits():
            break
        line_count = sum(line.count("\n") + 1 for line in group.lines)
        pointer = f"{group.name}: {line_count} lines omitted — {where}"
        if len(pointer) < sum(len(line) + 1 for line in current[group.name]):
            current[group.name] = [pointer]
            dropped.append(group.name)
    return dropped


def render_stdout(groups: list[MsgGroup], resumed: bool, overflow_file: Path) -> str:
    """The hook's stdout, kept under STDOUT_BUDGET with a meter line last.

    Over budget: the full text goes to `overflow_file` (best effort); every
    line is clipped to _LINE_CAP; degradable groups shrink to a pointer,
    lowest rank first, only while that helps; alert groups never degrade. If
    it still does not fit, the body is cut from the end on a line boundary.
    The meter names whatever was dropped or cut.
    """
    header = f"Session {'resumed' if resumed else 'started'}.\nBrief written to {BRIEF_FILE}."
    footer = _stdout_footer()
    limit = STDOUT_BUDGET - _METER_RESERVE

    def assemble(body: list[str]) -> str:
        return "\n".join([header, *body, "", footer])

    full = assemble([line for group in groups for line in group.lines])
    if len(full) <= limit:
        _clear_overflow(overflow_file)
        return f"{full}\nstdout: {len(full):,} / {STDOUT_BUDGET:,} chars"

    saved = _write_overflow(overflow_file, full)
    where = f"full text in {overflow_file}" if saved else "full text unavailable (overflow write failed)"
    current = {group.name: [_clip(line) for line in group.lines] for group in groups}
    clipped = any(len(line) > _LINE_CAP for group in groups for line in group.lines)

    def body_lines() -> list[str]:
        return [line for group in groups for line in current[group.name]]

    dropped = _degrade(groups, current, lambda: len(assemble(body_lines())) <= limit, where)

    body = body_lines()
    truncated = False
    while body and len(assemble(body)) > limit:
        body.pop()
        truncated = True
    out = assemble(body)

    notes = []
    if clipped:
        notes.append("lines clipped")
    if dropped:
        notes.append(f"dropped: {', '.join(dropped)}")
    if truncated:
        notes.append("body truncated")
    meter = f"stdout: {len(out):,} / {STDOUT_BUDGET:,} chars — {'; '.join(notes)}, {where}"
    return f"{out}\n{meter}"


def main():
    state, resumed = init_state()

    # Brain sync — count sessions, auto-push every N
    sync_msgs = brain_sync(state)

    # Memory decay — thermal decay + snapshot every session
    decay_msgs = memory_decay(state)

    # Evolve check — count instincts, nudge every N sessions
    evolve_msgs = evolve_check(state)

    # Config sync — warn if behind the remote, if live hooks are off the
    # configured branch, if the config checkout holds untracked files, if the
    # global secret scan is not running, or if a configured canary went stale
    config_sync_msgs = (
        config_sync_check() + live_hooks_branch() + config_untracked()
        + secret_scan_status() + canary_liveness()
    )

    # Dedup warning — detect dot-vs-hyphen encoding duplicates
    dedup_msgs = dedup_warning()

    # Brain check (backend connectivity), orphan drift (memory files whose
    # pg_id has no live entity, silent at 0) and the pending brief, run
    # concurrently: each waits on the backend, so an unreachable one costs one
    # timeout, not three.
    db = _run_db_checks()

    # Deployed-hook drift — silent unless CRAFT_CONFIG_REPO is set
    hook_msgs = hook_drift()

    write_state(state)  # persist updated counters

    brief = build_brief(state, resumed, pending=db.pending)
    BRIEF_FILE.parent.mkdir(parents=True, exist_ok=True)
    BRIEF_FILE.write_text(brief)

    # Output instructions for Claude via stdout, bounded under the hook cap.
    # Output order is unchanged; `rank` is the degradation order — cheapest to
    # lose first. Correctness alerts are never degradable.
    groups = build_groups(
        sync=sync_msgs,
        decay=decay_msgs,
        evolve=evolve_msgs,
        config=config_sync_msgs,
        dedup=dedup_msgs,
        brain=db.brain,
        orphans=db.orphans,
        hook_drift=hook_msgs,
    )
    print(render_stdout(groups, resumed, STDOUT_OVERFLOW_FILE))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[session_start] Error: {e}", file=sys.stderr)
        sys.exit(0)  # non-fatal — never block session start
