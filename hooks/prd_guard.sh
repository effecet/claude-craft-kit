#!/bin/bash
# ~/.claude/hooks/prd_guard.sh
# PreToolUse hook — fires before Write/Edit/MultiEdit tool calls.
# Reads the tool input JSON from stdin, checks if the file path
# looks production-related, and blocks with exit 2 if unconfirmed.

set -euo pipefail

INPUT=$(cat)

FILE_PATH=$(echo "$INPUT" | python3 -c "
import json, sys
d = json.load(sys.stdin)
# tool_input varies by tool — check common path fields
ti = d.get('tool_input', {})
print(ti.get('file_path', ti.get('path', ti.get('new_path', ''))))
" 2>/dev/null || echo "")

if [ -z "$FILE_PATH" ]; then
  exit 0
fi

# Check if path looks production-related
if echo "$FILE_PATH" | grep -qiE "(prd|prod|production)[/._-]"; then
  echo "⚠️  PRODUCTION FILE: $FILE_PATH" >&2
  echo "This file path looks production-related. Confirm before proceeding." >&2
  exit 2  # exit 2 = blocking error, stderr fed back to Claude
fi

exit 0
