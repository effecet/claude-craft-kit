#!/bin/bash
# ~/.claude/hooks/pr_security_scan.sh
# PreToolUse hook — fires before Bash tool calls that create PRs.
# Runs gitleaks on the full diff against the base branch to catch
# secrets that may have slipped through individual commits.

set -euo pipefail

INPUT=$(cat)

# Extract the command from tool input
COMMAND=$(echo "$INPUT" | python3 -c "
import json, sys
d = json.load(sys.stdin)
ti = d.get('tool_input', {})
print(ti.get('command', ''))
" 2>/dev/null || echo "")

# Only check PR creation commands
if ! echo "$COMMAND" | grep -qE "gh\s+pr\s+create"; then
  exit 0
fi

# Check if gitleaks is installed
if ! command -v gitleaks &>/dev/null; then
  echo "gitleaks not found — skipping pre-PR security scan" >&2
  exit 0
fi

# Detect base branch (main or master)
BASE_BRANCH=""
for candidate in main master; do
  if git rev-parse --verify "$candidate" &>/dev/null 2>&1; then
    BASE_BRANCH="$candidate"
    break
  fi
done

if [ -z "$BASE_BRANCH" ]; then
  echo "Could not detect base branch (main/master) — skipping pre-PR scan" >&2
  exit 0
fi

# Build gitleaks options
GITLEAKS_OPTS="--no-banner"
if [ -f ".gitleaks.toml" ]; then
  GITLEAKS_OPTS="$GITLEAKS_OPTS --config .gitleaks.toml"
elif [ -f ".gitleaks.yml" ]; then
  GITLEAKS_OPTS="$GITLEAKS_OPTS --config .gitleaks.yml"
fi

# Get the merge base to scan only the branch's changes
MERGE_BASE=$(git merge-base HEAD "$BASE_BRANCH" 2>/dev/null || echo "")
if [ -z "$MERGE_BASE" ]; then
  echo "Could not find merge base with $BASE_BRANCH — skipping pre-PR scan" >&2
  exit 0
fi

echo "Running pre-PR security scan (diff against $BASE_BRANCH)..." >&2

# Run gitleaks on the commit range between merge-base and HEAD
RESULT=$(gitleaks detect --log-opts="$MERGE_BASE..HEAD" $GITLEAKS_OPTS 2>&1) || true
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
  echo "" >&2
  echo "SECRETS DETECTED in branch diff ($MERGE_BASE..HEAD):" >&2
  echo "$RESULT" >&2
  echo "" >&2
  echo "PR creation blocked. Remove secrets from commits before creating PR." >&2
  exit 2
fi

echo "Pre-PR security scan passed." >&2
exit 0
