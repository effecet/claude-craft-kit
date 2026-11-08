# shellcheck shell=bash
# claude-craft-kit — example environment config
# Copy to config.local.sh (gitignored) and source it from your shell profile,
# or export these in your environment. Every hook reads from here; nothing is
# hardcoded to a specific machine or user.

# Where your Claude Code config lives (the hooks resolve paths from this).
export CLAUDE_DIR="${HOME}/.claude"

# Default Python interpreter the hooks shell out to.
export PYTHON_BIN="python3"

# ---- Desktop notifications (optional) ---------------------------------------
# track.py / on_idle.sh pop a desktop notification at the message threshold and
# on idle. Best-effort, cross-platform, silent no-op if the backend is missing:
#   macOS   : `terminal-notifier` (brew install terminal-notifier) — falls back
#             to the built-in `osascript`, so macOS works with no install.
#   Linux   : `notify-send`  (apt install libnotify-bin / dnf install libnotify)
#   Windows : PowerShell + BurntToast (Install-Module BurntToast)
# Nothing to set here — install the tool for your OS, or ignore for terminal-only.

# ---- Token report (optional) ------------------------------------------------
# track.py prints hooks/token_tracker.py's summary every few messages. Its cost
# line is an estimate at ONE flat price set for every model: set your model's
# prices in USD per million tokens. Its Context bar is rough too: tokens summed
# over the whole transcript, not the live context. Unset or invalid (not a
# finite positive number; the window must be greater than 1) = the defaults shown.
# export CRAFT_PRICE_INPUT_PER_M="15.00"
# export CRAFT_PRICE_OUTPUT_PER_M="75.00"
# export CRAFT_PRICE_CACHE_WRITE_PER_M="18.75"
# export CRAFT_PRICE_CACHE_READ_PER_M="1.50"
# export CRAFT_CONTEXT_WINDOW="1000000"

# ---- Optional memory backend (OFF by default) -------------------------------
# The brain-coupled hooks (proactive_recall, backup_hook, and the brain steps
# in session_start/track) stay dormant unless you opt in here. Wiring guide:
# docs/WORKFLOW.md. A reference backend: https://github.com/effecet/memory-persistor
export BRAIN_ENABLED="false"

# If BRAIN_ENABLED=true, point recall at your Postgres-backed memory store:
# export DATABASE_URL="postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres"

# Local checkout of the backend repo (default: ~/Desktop or ~/Downloads/memory-backend).
# session_start uses its Makefile for the connectivity check and imports its
# scripts/memory_dedup.py for the orphan-memory drift line — so that checkout's
# code runs at every session start. Point this only at a checkout you trust.
# export MEMORY_REPO_DIR="$HOME/src/memory-persistor"

# The name of your memory MCP tool used in the memory rules (rules/memory.md).
export MEMORY_REMEMBER_TOOL="mcp__memory__remember"

# ---- Optional git backup (OFF unless BRAIN_ENABLED=true) --------------------
export BACKUP_REMOTE="origin"
export BACKUP_BRANCH="main"
# Optional DB dump alongside the git backup (unset = skipped):
# export BACKUP_DB_URL="postgresql://..."

# ---- Optional session-brief checks (all OFF until set) ----------------------
# session_start.py can warn about problems that otherwise stay silent. Each
# check below does nothing unless its setting is present.
#
# CRAFT_CONFIG_REPO turns on three checks. Deployed-hook drift: compare
# ~/.claude/hooks and ~/.claude/scripts with the git checkout they come from, and say which copies are AHEAD (local forks —
# upstream them before copying), stale, or diverged.
# export CRAFT_CONFIG_REPO="${HOME}/code/claude-craft-kit"
# It also tells validate_tier.py which checkout is your config repo: there,
# the root hooks/session_start.py and tests/test_session_start*.py skip the
# `session` sensitive pattern (other patterns and the credential check still
# apply). Elsewhere those generic paths stay Full. This kit's own clone counts
# without it.
# Subdirectory of that checkout holding hooks/ and scripts/ (unset = root):
# export CRAFT_CONFIG_SUBDIR=""
# Basenames you keep as deliberate per-machine forks (comma-separated):
# export CRAFT_HOOK_DRIFT_EXCLUDE=""
#
# Live-hooks branch + untracked config (also enabled by CRAFT_CONFIG_REPO):
# when ~/.claude/hooks is a symlink into that checkout, warn if it is not on
# this branch, or has untracked files.
# export CRAFT_CONFIG_BRANCH="main"
#
# Stale heartbeat: a scheduled job writes `date +%s` to this file every run;
# warn when it is older than CRAFT_CANARY_MAX_AGE_H hours.
# export CRAFT_CANARY_FILE="${HOME}/.claude/session/canary_last_run"
# export CRAFT_CANARY_MAX_AGE_H="3"
#
# Secret scan: the global git hooks dir core.hooksPath must point at
# (`make install-git-hooks` installs git-hooks/ there; the check also verifies
# a recent enough gitleaks is installed).
# export CRAFT_GIT_HOOKS_DIR="${HOME}/.claude/git-hooks"
