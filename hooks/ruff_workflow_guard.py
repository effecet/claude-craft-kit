#!/usr/bin/env python3
"""
ruff_workflow_guard.py

PreToolUse:Bash hook — blocks `git commit` (exit 2) when CWD is a
workflow-bearing repo with ruff config and ruff finds issues.

Limitation: uses `Path.cwd()` (harness CWD). Commands prefixed with
`cd /other && git commit ...` won't be checked against `/other` — only
the harness CWD. Acceptable: the common case is committing in CWD.

Why structural enforcement:
"Run the CI mirror before pushing" is easy to skip under time pressure.
A pre-commit gate catches `ruff check` and `ruff format` failures locally
instead of post-push CI.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

GIT_COMMIT_RE = re.compile(
    r"(^|[\s;&|(\"'])git\s+(?:[^;&|\n]*?\s+)?commit(?=[^\w-]|$)"
)
WORKFLOW_DIRS = (".forgejo/workflows", ".github/workflows")


def _is_git_commit(command: str) -> bool:
    return bool(GIT_COMMIT_RE.search(command))


def _has_workflow_dir(cwd: Path) -> bool:
    return any((cwd / d).is_dir() for d in WORKFLOW_DIRS)


def _has_ruff_config(cwd: Path) -> bool:
    pyproject = cwd / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        with pyproject.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return "ruff" in data.get("tool", {})


def _run_ruff(cwd: Path) -> tuple[int, str]:
    """Run ruff check + ruff format --check. Return (exit_code, combined_output).

    Returns (0, "") on success. Returns (nonzero, output) on ruff failure.
    On infra errors (FileNotFoundError, TimeoutExpired) logs to stderr and
    returns (0, "") — fail-open, but loudly (a silent guard is a useless guard).
    """
    parts: list[str] = []
    for args in (["ruff", "check", "."], ["ruff", "format", "--check", "."]):
        try:
            proc = subprocess.run(
                args,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            print(f"ruff_workflow_guard: ruff invocation failed: {e}", file=sys.stderr)
            return 0, ""
        if proc.returncode != 0:
            parts.append(f"$ {' '.join(args)}")
            if proc.stdout.strip():
                parts.append(proc.stdout.rstrip())
            if proc.stderr.strip():
                parts.append(proc.stderr.rstrip())
            return proc.returncode, "\n".join(parts)
    return 0, ""


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    if not isinstance(data, dict):
        return 0

    if data.get("tool_name") != "Bash":
        return 0

    command = (data.get("tool_input") or {}).get("command", "")
    if not isinstance(command, str) or not _is_git_commit(command):
        return 0

    cwd = Path.cwd()
    if not _has_workflow_dir(cwd):
        return 0
    if not _has_ruff_config(cwd):
        return 0
    if shutil.which("ruff") is None:
        return 0

    code, output = _run_ruff(cwd)
    if code == 0:
        return 0

    print(
        f"RUFF PRE-COMMIT GATE FAILED in {cwd}\n\n"
        f"{output}\n\n"
        f"Workflow-bearing repo detected ({', '.join(WORKFLOW_DIRS)}); "
        f"the same ruff run will fail in CI. Fix locally with:\n"
        f"  ruff check . --fix\n"
        f"  ruff format .\n"
        f"then re-stage and re-commit. Bypass with --no-verify is NOT honored "
        f"(this is a Claude harness PreToolUse hook, not a git pre-commit).",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
