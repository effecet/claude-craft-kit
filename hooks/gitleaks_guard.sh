#!/bin/bash
# ~/.claude/hooks/gitleaks_guard.sh
# PreToolUse hook — fires before Bash tool calls that run git commit.
# Runs gitleaks on staged changes to catch secrets before they're committed.

set -euo pipefail

INPUT=$(cat)

# Extract the command from tool input
COMMAND=$(echo "$INPUT" | python3 -c "
import json, sys
d = json.load(sys.stdin)
ti = d.get('tool_input', {})
print(ti.get('command', ''))
" 2>/dev/null || echo "")

# Only check git commit commands
if ! echo "$COMMAND" | grep -qE "git\s+commit"; then
  exit 0
fi

# Check if gitleaks is installed
if ! command -v gitleaks &>/dev/null; then
  echo "gitleaks not found — skipping secret scan" >&2
  exit 0
fi

# Build gitleaks options — pick up per-project config if available
GITLEAKS_OPTS="--staged --no-banner"
if [ -f ".gitleaks.toml" ]; then
  GITLEAKS_OPTS="$GITLEAKS_OPTS --config .gitleaks.toml"
elif [ -f ".gitleaks.yml" ]; then
  GITLEAKS_OPTS="$GITLEAKS_OPTS --config .gitleaks.yml"
fi

# Run gitleaks on staged changes
RESULT=$(gitleaks protect $GITLEAKS_OPTS 2>&1) || true
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
  echo "🔴 SECRETS DETECTED in staged files:" >&2
  echo "$RESULT" >&2
  echo "" >&2
  echo "Commit blocked. Remove secrets before committing." >&2
  exit 2
fi

exit 0
