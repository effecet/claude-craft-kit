#!/bin/bash
# ~/.claude/hooks/on_idle.sh
#
# Called when system idle is detected (via hooks or external trigger).
# 1. Guards against stale triggers by re-reading state.json
# 2. Writes pending_prompt.json so Claude Code surfaces the offer on next message
# 3. Sends a desktop notification (terminal-notifier on macOS, notify-send on Linux)
# 4. Marks idle_prompted in state.json

set -euo pipefail

STATE_FILE="$HOME/.claude/session/state.json"
PROMPT_FILE="$HOME/.claude/session/pending_prompt.json"
LOG_FILE="$HOME/.claude/session/watcher.log"

log() {
  echo "[on_idle] $(date '+%Y-%m-%d %H:%M:%S')  $1" | tee -a "$LOG_FILE"
}

# ── Guard ─────────────────────────────────────────────────────────────────────
# watcher.py checks state before calling us, but on_idle.sh may also be called
# from hooks.yaml directly — so we re-check here to be safe.

if [ ! -f "$STATE_FILE" ]; then
  log "No state file. Exiting."
  exit 0
fi

read_flag() {
  python3 -c "
import json, sys
with open('$STATE_FILE') as f:
    s = json.load(f)
print('true' if s.get('$1', False) else 'false')
" 2>/dev/null || echo "true"  # fail safe: if unreadable, don't trigger
}

MSG_COUNT=$(python3 -c "
import json, sys
with open('$STATE_FILE') as f:
    s = json.load(f)
print(s.get('user_message_count', 0))
" 2>/dev/null || echo "0")

if [ "$(read_flag wrap_up_ran)" = "true" ]; then
  log "wrap_up_ran=true. Exiting."
  exit 0
fi

if [ "$(read_flag idle_prompted)" = "true" ]; then
  log "idle_prompted=true. Exiting."
  exit 0
fi

if [ "$MSG_COUNT" -eq 0 ]; then
  log "No messages in session. Exiting."
  exit 0
fi

# ── Write pending prompt ──────────────────────────────────────────────────────
# Claude Code's consume-pending-prompt hook (hooks.yaml) reads this file
# on the user's next message and surfaces the wrap-up offer in-conversation.

log "Writing pending_prompt.json"

python3 -c "
import json
from datetime import datetime
prompt = {
    'type': 'idle',
    'created_at': datetime.now(__import__('datetime').UTC).isoformat(),
    'message': 'You have been inactive for a while. Want to wrap up this session?',
    'skill': 'wrap-up',
    'trigger': 'idle'
}
with open('$PROMPT_FILE', 'w') as f:
    json.dump(prompt, f, indent=2)
"

# ── Update state ──────────────────────────────────────────────────────────────

python3 -c "
import json
with open('$STATE_FILE') as f:
    s = json.load(f)
s['idle_prompted'] = True
with open('$STATE_FILE', 'w') as f:
    json.dump(s, f, indent=2)
" && log "State: idle_prompted=true" || log "Warning: could not update state"

# ── Desktop notification ──────────────────────────────────────────────────────
# Surfaces outside the terminal so you notice even when Claude Code is in bg.
# Deps: macOS uses `terminal-notifier` (brew install terminal-notifier) and falls
# back to the built-in `osascript` if it's absent — so macOS needs no install.
# Linux uses `notify-send` (apt install libnotify-bin). Every call is best-effort
# (`|| true`), so a missing tool never aborts the hook.

MSG="You have been idle for 1 hour. Open Claude Code to wrap up."
if [[ "$(uname)" == "Darwin" ]]; then
  if command -v terminal-notifier >/dev/null 2>&1; then
    terminal-notifier -title "Claude Code" -subtitle "Session idle" -message "$MSG" -sound default 2>/dev/null || true
  elif command -v osascript >/dev/null 2>&1; then
    osascript -e "display notification \"$MSG\" with title \"Claude Code\"" 2>/dev/null || true
  fi
else
  command -v notify-send >/dev/null 2>&1 && \
    notify-send "Claude Code" "$MSG" --icon=dialog-information --urgency=normal 2>/dev/null || true
fi

log "on_idle complete."
