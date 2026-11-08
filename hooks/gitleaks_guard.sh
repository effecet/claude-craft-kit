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

# Only check git commit commands, options before `commit` included
# (`git -C x commit`, `git -c k=v commit`). Aliases (`git ci`) are not seen:
# this hook is advisory, and a git pre-commit hook is the real gate.
# Bash's own =~, no subprocess: `echo | grep -q` under pipefail read a match as
# a miss once a long command made echo die of SIGPIPE, and a here-string needs
# a temp file, which fails open where none can be created.
COMMIT_RE='(^|[^[:alnum:]_])git([[:space:]]+[^;&|]*)?[[:space:]]commit([^[:alnum:]_-]|$)'
if ! [[ "$COMMAND" =~ $COMMIT_RE ]]; then
  exit 0
fi

# Check if gitleaks is installed
if ! command -v gitleaks &>/dev/null; then
  echo "gitleaks not found — skipping secret scan" >&2
  exit 0
fi

# Scan the repo's top level: from a subdirectory, `gitleaks git` with no path
# scans 0 bytes and reports "no leaks found".
# This is the repo of the hook's working directory: `git -C /other commit` or
# `cd /other && git commit` is not followed there (advisory; see README).
if ! TOP=$(git rev-parse --show-toplevel 2>/dev/null); then
  echo "gitleaks_guard: not inside a git repo here — skipping secret scan" >&2
  exit 0
fi

# Build gitleaks options — pick up the repo's config if available.
# --exit-code 3 tells a leak (3) from a gitleaks error (1); --redact keeps a
# found secret out of the output, which goes back to the model.
GITLEAKS_OPTS=(--staged --no-banner --redact --exit-code 3)
if [ -f "$TOP/.gitleaks.toml" ]; then
  GITLEAKS_OPTS+=(--config "$TOP/.gitleaks.toml")
elif [ -f "$TOP/.gitleaks.yml" ]; then
  GITLEAKS_OPTS+=(--config "$TOP/.gitleaks.yml")
fi

# Run gitleaks on the staged changes of the current directory's repo. The old
# `RESULT=$(...) || true` made the status always 0, so this hook never blocked.
EXIT_CODE=0
RESULT=$(gitleaks git "${GITLEAKS_OPTS[@]}" "$TOP" 2>&1) || EXIT_CODE=$?

if [ "$EXIT_CODE" -eq 3 ]; then
  echo "🔴 SECRETS DETECTED in staged files:" >&2
  echo "$RESULT" >&2
  echo "" >&2
  echo "Commit blocked. Remove secrets before committing." >&2
  exit 2
elif [ "$EXIT_CODE" -ne 0 ]; then
  # Fail closed: a scan that did not complete is not a clean scan.
  echo "🔴 gitleaks failed (rc=$EXIT_CODE), commit blocked:" >&2
  echo "$RESULT" >&2
  exit 2
fi

exit 0
