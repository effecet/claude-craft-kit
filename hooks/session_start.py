#!/usr/bin/env python3
"""
SessionStart hook — ~/.claude/session_start.py
Runs once when Claude Code starts a session.

Responsibilities:
  1. Initialize or resume state.json
  2. Collect context (git status, pending items, recent learnings)
  3. Write brief.md for Claude to read and present as orientation
"""

import json
import os
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from _state import make_fresh_state

CLAUDE_DIR = Path(os.environ.get("CLAUDE_DIR", str(Path.home() / ".claude")))
STATE_FILE = CLAUDE_DIR / "session/state.json"
BRIEF_FILE = CLAUDE_DIR / "session/brief.md"
PENDING_FILE = CLAUDE_DIR / "pending.md"
LEARNINGS = CLAUDE_DIR / "LEARNINGS.md"
SPEC_AUDIT = CLAUDE_DIR / "hooks/spec_audit.py"

MACHINE_ID = socket.gethostname().split(".")[0]  # short hostname for commit tags

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
        result = subprocess.run(
            ["make", "-C", str(MEMORY_REPO_DIR), "status-remote"],
            capture_output=True,
            text=True,
            timeout=10,
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
        msgs.append("🧠 Brain: ❌ memory backend check timed out (10s)")
    except Exception as e:
        msgs.append(f"🧠 Brain: ⚠️ check failed — {e}")

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


# ── Brief builder ─────────────────────────────────────────────────────────────


def build_brief(state: dict, resumed: bool) -> str:
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
    if PENDING_FILE.exists():
        content = PENDING_FILE.read_text().strip()
        # Strip header lines
        lines = [l for l in content.splitlines() if not l.startswith("#")]
        content = "\n".join(lines).strip()
        sections.append(f"## Pending\n{content if content else 'None.'}\n")
    else:
        sections.append("## Pending\nNone.\n")

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


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    state, resumed = init_state()

    # Brain sync — count sessions, auto-push every N
    sync_msgs = brain_sync(state)

    # Memory decay — thermal decay + snapshot every session
    decay_msgs = memory_decay(state)

    # Evolve check — count instincts, nudge every N sessions
    evolve_msgs = evolve_check(state)

    # Config sync — warn if behind Codeberg
    config_sync_msgs = config_sync_check()

    # Dedup warning — detect dot-vs-hyphen encoding duplicates
    dedup_msgs = dedup_warning()

    # Brain check — verify Supabase connectivity
    brain_msgs = brain_check()

    write_state(state)  # persist updated counters

    brief = build_brief(state, resumed)
    BRIEF_FILE.parent.mkdir(parents=True, exist_ok=True)
    BRIEF_FILE.write_text(brief)

    # Output instructions for Claude via stdout
    # Claude Code passes hook stdout as context
    all_msgs = (
        sync_msgs
        + decay_msgs
        + evolve_msgs
        + config_sync_msgs
        + dedup_msgs
        + brain_msgs
    )
    sync_output = "\n".join(all_msgs) if all_msgs else ""
    print(
        f"""
Session {"resumed" if resumed else "started"}.
Brief written to {BRIEF_FILE}.
{sync_output}

Read {BRIEF_FILE} and present a session orientation (max 10 lines).
Lead with the most actionable item: pending items > dirty repos > last session gap.
If everything is clean, say so briefly. Tone: oriented co-pilot, not a greeter.
""".strip()
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[session_start] Error: {e}", file=sys.stderr)
        sys.exit(0)  # non-fatal — never block session start
