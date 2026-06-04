#!/usr/bin/env python3
"""
UserPromptSubmit hook — ~/.claude/track.py

Called on every user message via hooks in settings.json.
Receives JSON on stdin from Claude Code containing session metadata.

Responsibilities:
  1. Increment user_message_count, update last_active
  2. Extract transcript_path and store in state for token_tracker
  3. Every TOKEN_REPORT_INTERVAL messages: run token_tracker and print summary
  4. Detect goodbye patterns in user prompt → trigger auto wrap-up

NOT intended to be run manually.
"""

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from _state import make_fresh_state
from common import encode_project_path

CLAUDE_DIR = Path(os.environ.get("CLAUDE_DIR", str(Path.home() / ".claude")))
STATE_FILE = CLAUDE_DIR / "session/state.json"
FRICTION_LOG = CLAUDE_DIR / "session/friction.jsonl"
TOKEN_TRACKER = CLAUDE_DIR / "token_tracker.py"
BACKUP_SCRIPT = CLAUDE_DIR / "hooks/backup_hook.py"
PYTHON_VENV = Path.home() / ".venv/bin/python"

# When false (default), the optional intra-session backup and the
# memory-backend staleness check are skipped. Message counting, token
# reporting, friction/goodbye detection, and state always run.
# optional: wire your own memory backend here (see docs/WORKFLOW.md)
BRAIN_ENABLED = os.environ.get("BRAIN_ENABLED", "false").lower() == "true"

WRAP_UP_MIN_MESSAGES = 10  # minimum messages before wrap-up is meaningful
TOKEN_REPORT_INTERVAL = 5  # print token summary every N messages
MSG_THRESHOLD = 50  # prompt wrap-up after this many messages
BACKUP_INTERVAL = 25  # fire optional intra-session backup every N messages

# Goodbye patterns — matched against the full message (case-insensitive).
# Only triggers when the message IS a goodbye, not when it contains one mid-sentence.
_GOODBYE_PATTERNS = re.compile(
    r"^\s*("
    r"goodbye|good bye|bye|bye bye|ciao|chau|buenas noches|buona notte"
    r"|that'?s all|that'?s it|we'?re done|i'?m done|done for now|done for today"
    r"|end session|wrap it up|wrap up|let'?s wrap|wrapup"
    r"|thanks,?\s*that'?s all|gracias|grazie"
    r"|see you|nos vemos|ci vediamo"
    r"|good night|buona sera"
    r"|signing off|over and out"
    r")\s*[!.?]*\s*$",
    re.IGNORECASE,
)

# Friction patterns — detect user corrections / frustration mid-session.
# Conservative: only fire on patterns that are NOT plausibly friendly conversation.
# Pattern → label tuple. Order matters; first match wins.
_FRICTION_PATTERNS = [
    # Fire on "no" at the start ONLY when followed by a correction word.
    # Skips benign phrases: "no problem", "no way", "no worries", "no thanks",
    # "no kidding", "no doubt". Also skips "now" (no trailing punct required).
    (
        re.compile(
            r"^\s*no[\s,.!]+(?!(?:problem|way|worries|thanks|thank you|kidding|doubt|rush|hurry)\b)"
            r"(?:that|it|this|you|we|i|don'?t|not|it'?s|that'?s)",
            re.IGNORECASE,
        ),
        "no_start",
    ),  # "no, that's wrong" / "no don't" but not "no problem" / "now"
    (re.compile(r"^\s*wait[\s,.!]", re.IGNORECASE), "wait_start"),
    (re.compile(r"^\s*stop[\s,.!]", re.IGNORECASE), "stop_start"),
    (re.compile(r"^\s*actually[\s,.!]", re.IGNORECASE), "actually_start"),
    (re.compile(r"\b(doesn'?t work|didn'?t work)\b", re.IGNORECASE), "doesnt_work"),
    (
        re.compile(r"\bstill (broken|failing|wrong|not working)\b", re.IGNORECASE),
        "still_broken",
    ),
    (re.compile(r"\byou (forgot|missed|skipped)\b", re.IGNORECASE), "you_missed"),
    (
        re.compile(
            r"\b(let me try again|let's try again|redo|do it again)\b", re.IGNORECASE
        ),
        "retry",
    ),
    (re.compile(r"\bthat'?s (wrong|not right|incorrect)\b", re.IGNORECASE), "wrong"),
]


def _detect_friction(prompt: str) -> str | None:
    """Return a friction label if the prompt looks like a user correction, else None."""
    if not prompt:
        return None
    for pattern, label in _FRICTION_PATTERNS:
        if pattern.search(prompt):
            return label
    return None


def _log_friction(label: str, prompt: str, msg_count: int) -> None:
    """Append a friction event to ~/.claude/session/friction.jsonl (best-effort)."""
    try:
        FRICTION_LOG.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "v": 1,
            "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "type": "user_correction",
            "label": label,
            "msg_count": msg_count,
            "snippet": prompt.strip()[:160],
        }
        with open(FRICTION_LOG, "a") as f:
            f.write(json.dumps(event) + "\n")
    except Exception:
        pass  # never block the hook


def _desktop_notify(title: str, message: str, urgency: str = "normal") -> None:
    """Best-effort cross-platform desktop notification. Always a silent no-op if
    no notifier is available — it never raises, so a missing tool can't break the
    hook.

    Backend per OS (with install hints):
      macOS   : `terminal-notifier` if installed (`brew install terminal-notifier`),
                otherwise the built-in `osascript` — so macOS works with zero install.
      Linux   : `notify-send` (`apt install libnotify-bin` / `dnf install libnotify`).
      Windows : PowerShell + the BurntToast module (`Install-Module BurntToast`).
                If BurntToast isn't present the call no-ops; swap in your own
                toast command here if you prefer a different mechanism.
    """
    system = platform.system()
    try:
        if system == "Darwin":
            if shutil.which("terminal-notifier"):
                subprocess.run(
                    ["terminal-notifier", "-title", title, "-message", message, "-sound", "Pop"],
                    timeout=5, capture_output=True,
                )
            elif shutil.which("osascript"):  # built-in fallback, no install needed
                script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
                subprocess.run(["osascript", "-e", script], timeout=5, capture_output=True)
        elif system == "Windows":
            if shutil.which("powershell"):
                def _ps(s: str) -> str:  # PowerShell single-quote escaping
                    return "'" + s.replace("'", "''") + "'"
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     f"New-BurntToastNotification -Text {_ps(title)}, {_ps(message)}"],
                    timeout=5, capture_output=True,
                )
        else:  # Linux / other
            if shutil.which("notify-send"):
                subprocess.run(
                    ["notify-send", title, message, "--icon=dialog-information", f"--urgency={urgency}"],
                    timeout=5, capture_output=True,
                )
    except Exception:
        pass


def read_state() -> dict:
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def write_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _archive_and_reset_if_day_changed(state: dict) -> dict:
    """If state.session_id is from a prior calendar day, archive it and return a fresh state.

    session_start.py has the same check but only fires on new sessions. If the user keeps
    Claude Code open across midnight, track.py would otherwise keep incrementing flags from
    yesterday's session. This catches that case.
    """
    today = time.strftime("%Y-%m-%d", time.gmtime())
    sid = state.get("session_id", "")
    if not sid or sid[:10] == today:
        return state
    try:
        archive_dir = STATE_FILE.parent / "state-archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        # Use the OLD session date for the archive filename
        old_date = sid[:10]
        archive_path = archive_dir / f"{old_date}.json"
        # If today already has an archive, append a counter
        i = 1
        while archive_path.exists():
            archive_path = archive_dir / f"{old_date}.{i}.json"
            i += 1
        with open(archive_path, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass
    # Fresh state for the new day. Preserve transcript_path and cwd since they
    # describe the current live session, not yesterday's.
    return make_fresh_state(preserved=state)


def run_token_report(transcript_path: str) -> None:
    # Guard: token_tracker.py is an optional sibling. Degrade gracefully if absent.
    if not TOKEN_TRACKER.exists():
        return
    try:
        subprocess.run(["python3", str(TOKEN_TRACKER), transcript_path], timeout=10)
    except Exception as e:
        print(f"[track] Token report error: {e}", file=sys.stderr)


def _detect_cwd() -> str:
    """Best-effort CWD detection from environment."""
    return os.environ.get("PWD", os.getcwd())


def _discover_transcript() -> str:
    """Find the most recently modified .jsonl transcript in the Claude projects dir."""
    cwd = _detect_cwd()
    encoded = encode_project_path(cwd)
    projects_dir = CLAUDE_DIR / "projects" / encoded
    if not projects_dir.is_dir():
        return ""
    jsonl_files = list(projects_dir.glob("*.jsonl"))
    if not jsonl_files:
        return ""
    # Most recently modified = current session's transcript
    latest = max(jsonl_files, key=lambda f: f.stat().st_mtime)
    return str(latest)


def _rotate_learnings_if_needed(learnings: Path) -> None:
    """Archive LEARNINGS.md to LEARNINGS/YYYY-MM.md when the month rolls over.

    Looks at the last ``## [YYYY-MM-DD]`` header in the file. If its month
    differs from the current month, move the file into the archive directory
    named by the OLD month and leave a fresh empty LEARNINGS.md behind.
    Silent no-op if the file is missing, empty, or has no parseable headers.
    """
    try:
        if not learnings.exists() or learnings.stat().st_size == 0:
            return
        last_month = None
        with open(learnings) as f:
            for line in f:
                m = re.match(r"^##\s*\[(\d{4})-(\d{2})-\d{2}\]", line)
                if m:
                    last_month = f"{m.group(1)}-{m.group(2)}"
        if not last_month:
            return
        current_month = time.strftime("%Y-%m")
        if last_month == current_month:
            return
        archive_dir = learnings.parent / "LEARNINGS"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / f"{last_month}.md"
        # If archive already exists (rotation attempted before), append instead
        if archive_path.exists():
            archive_path.write_text(
                archive_path.read_text() + "\n" + learnings.read_text()
            )
            learnings.unlink()
        else:
            learnings.rename(archive_path)
        learnings.touch()
    except Exception:
        pass  # best-effort; never block the hook


def _git_files_touched(cwd: str | None) -> list[str]:
    """Return list of modified/untracked files in a git repo (best-effort)."""
    if not cwd:
        return []
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "diff", "--name-only", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        files = [f for f in result.stdout.strip().splitlines() if f]
        # Also grab untracked files
        result2 = subprocess.run(
            ["git", "-C", cwd, "ls-files", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        files += [f for f in result2.stdout.strip().splitlines() if f]
        return files
    except Exception:
        return []


def _parse_hook_input() -> dict:
    """Read hook input JSON from stdin (Claude Code session metadata)."""
    try:
        raw = sys.stdin.read()
        if raw.strip():
            return json.loads(raw)
    except Exception:
        pass
    return {}


def _resolve_transcript(hook_data: dict, state: dict) -> str:
    """Resolve transcript path: stdin fields > state cache > auto-discover."""
    tp = (
        hook_data.get("transcript_path")
        or hook_data.get("transcriptPath")
        or hook_data.get("session_transcript_path")
        or ""
    )
    if not tp:
        tp = state.get("transcript_path", "")
    if not tp:
        tp = _discover_transcript()
    return tp


def _step_token_report(count: int, transcript_path: str) -> None:
    """Print token summary every TOKEN_REPORT_INTERVAL messages."""
    if count % TOKEN_REPORT_INTERVAL != 0:
        return
    if transcript_path:
        run_token_report(transcript_path)
    else:
        print(f"[i] Session checkpoint: {count} messages")
    _desktop_notify("Claude Code", f"Session checkpoint: {count} messages")


def _step_backup(count: int) -> None:
    """Fire an optional intra-session backup every BACKUP_INTERVAL messages.

    Gated behind BRAIN_ENABLED — no-op when the optional brain is disabled.
    The backup script (backup_hook.py) is part of your own setup, if any.
    # optional: wire your own memory backend here (see docs/WORKFLOW.md)
    """
    if not BRAIN_ENABLED:
        return
    if count <= 0 or count % BACKUP_INTERVAL != 0 or not BACKUP_SCRIPT.exists():
        return
    try:
        python_bin = str(PYTHON_VENV) if PYTHON_VENV.exists() else "python3"
        subprocess.Popen(
            [python_bin, str(BACKUP_SCRIPT)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _step_threshold_prompt(count: int, state: dict) -> None:
    """Write pending_prompt.json when message count hits MSG_THRESHOLD."""
    if count != MSG_THRESHOLD:
        return
    if state.get("wrap_up_ran") or state.get("wrap_up_prompted") or state.get("threshold_prompted"):
        return
    prompt_file = STATE_FILE.parent / "pending_prompt.json"
    prompt_data = {
        "type": "message_threshold",
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "message": "We've hit a good stopping point. Want to wrap up, or keep going?",
        "skill": "wrap-up",
        "trigger": "message_threshold",
    }
    with open(prompt_file, "w") as f:
        json.dump(prompt_data, f, indent=2)
    _desktop_notify("Claude Code", f"Session at {count} messages — consider wrapping up")
    state["threshold_prompted"] = True


def _step_friction(user_prompt: str, count: int) -> None:
    """Log user corrections to friction.jsonl."""
    label = _detect_friction(user_prompt)
    if label:
        _log_friction(label, user_prompt, count)


def _step_goodbye(user_prompt: str, count: int, state: dict) -> None:
    """Surface a wrap-up reminder on goodbye patterns."""
    if not user_prompt or count < WRAP_UP_MIN_MESSAGES:
        return
    if state.get("wrap_up_ran") or state.get("wrap_up_prompted"):
        return
    if not _GOODBYE_PATTERNS.match(user_prompt):
        return
    print(
        "REMINDER: the user's message looks like a goodbye. "
        "Before processing it normally, ASK ONCE: "
        '"Want me to run the wrap-up sequence (ship work, capture memories, '
        'review) before we close, or just close out?" Then wait for their answer. '
        "If they confirm → invoke the wrap-up skill (trigger: session_end). "
        "If they decline or want to keep going → acknowledge and continue normally. "
        "Do NOT force wrap-up. This reminder fires at most once per session."
    )
    _desktop_notify("Claude Code", "Wrap-up reminder — goodbye detected")
    state["wrap_up_prompted"] = True


def _step_staleness(count: int, state: dict) -> None:
    """On first user message of a session, check if an optional memory-backend
    repo is behind its origin. Emits a <system-reminder> when behind; never
    blocks. Advisory only.

    Gated behind BRAIN_ENABLED — no-op when the optional brain is disabled.
    The staleness_check sibling is part of your own setup, if any; the import
    is guarded so the hook degrades gracefully when it's absent.
    # optional: wire your own memory backend here (see docs/WORKFLOW.md)
    """
    if not BRAIN_ENABLED:
        return
    if count != 1:
        return
    if state.get("staleness_prompted"):
        return
    try:
        import staleness_check as _sc
        _repo = _sc.resolve_memory_repo()
        if _repo is None:
            return
        _result = _sc.check_staleness(_repo)
        if _result is not None:
            print(f"<system-reminder>\n{_sc.format_reminder(_result)}\n</system-reminder>")
            state["staleness_prompted"] = True
    except Exception:
        pass  # Advisory only — never blocks session


def _step_breadcrumb(count: int, state: dict) -> None:
    """Append session breadcrumb to LEARNINGS.md on first threshold cross."""
    if count != WRAP_UP_MIN_MESSAGES or state.get("breadcrumb_written"):
        return
    try:
        learnings = CLAUDE_DIR / "LEARNINGS.md"
        _rotate_learnings_if_needed(learnings)
        session_id = state.get("session_id", "unknown")
        cwd = state.get("cwd") or _detect_cwd()
        files_touched = _git_files_touched(cwd)

        with open(learnings, "a") as f:
            f.write(f"\n## [{time.strftime('%Y-%m-%d')}] Session breadcrumb\n")
            f.write(f"**Session:** {session_id}\n")
            f.write(f"**Directory:** {cwd or 'unknown'}\n")
            f.write(f"**Messages:** {count}\n")
            if files_touched:
                f.write("**Files touched:**\n")
                for fp in files_touched[:15]:
                    f.write(f"  - {fp}\n")
            else:
                f.write("**Files touched:** none detected\n")
        state["breadcrumb_written"] = True
    except Exception:
        pass


def main() -> None:
    hook_data = _parse_hook_input()
    state = read_state()
    state = _archive_and_reset_if_day_changed(state)

    transcript_path = _resolve_transcript(hook_data, state)
    count = state.get("user_message_count", 0) + 1
    state["user_message_count"] = count
    state["last_active"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    if transcript_path:
        state["transcript_path"] = transcript_path

    user_prompt = hook_data.get("prompt", "")

    _step_token_report(count, transcript_path)
    _step_backup(count)
    _step_staleness(count, state)
    _step_threshold_prompt(count, state)
    _step_friction(user_prompt, count)
    _step_goodbye(user_prompt, count, state)
    _step_breadcrumb(count, state)

    write_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[track] Error: {e}", file=sys.stderr)
        sys.exit(0)  # non-fatal
