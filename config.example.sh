# claude-craft-kit — example environment config
# Copy to config.local.sh (gitignored) and source it from your shell profile,
# or export these in your environment. Every hook reads from here; nothing is
# hardcoded to a specific machine or user.

# Where your Claude Code config lives (the hooks resolve paths from this).
export CLAUDE_DIR="${HOME}/.claude"

# Default Python interpreter the hooks shell out to.
export PYTHON_BIN="python3"

# ---- Optional memory backend (OFF by default) -------------------------------
# The brain-coupled hooks (proactive_recall, backup_hook, and the brain steps
# in session_start/track) stay dormant unless you opt in here. Wiring guide:
# docs/WORKFLOW.md. A reference backend: https://github.com/effecet/memory-persistor
export BRAIN_ENABLED="false"

# If BRAIN_ENABLED=true, point recall at your Postgres-backed memory store:
# export DATABASE_URL="postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres"

# The name of your memory MCP tool used in the memory rules (rules/memory.md).
export MEMORY_REMEMBER_TOOL="mcp__memory__remember"

# ---- Optional git backup (OFF unless BRAIN_ENABLED=true) --------------------
export BACKUP_REMOTE="origin"
export BACKUP_BRANCH="main"
# Optional DB dump alongside the git backup (unset = skipped):
# export BACKUP_DB_URL="postgresql://..."
