#!/usr/bin/env python3
"""
~/.claude/hooks/hook_bypass_guard.py

PreToolUse:Bash hook. Blocks (exit 2) a Bash command that would switch off
the global git pre-commit hook, which is what scans every commit for secrets
(git-hooks/dispatch, installed by `make install-git-hooks`):

  - `git commit --no-verify`, `-n`, `-n` inside a short-flag cluster (`-anm`),
    and the abbreviations git accepts (`--no-verif`, `--no-v`);
  - core.hooksPath set or unset by `git -c`, `--config-env`, `git config`
    (old and new syntax, including removing or renaming the [core] section)
    or a `GIT_CONFIG_*` environment variable;
  - `GIT_CONFIG_GLOBAL=...` or `HOME=...` prefixed to a commit, either of which
    makes git skip the global config that holds core.hooksPath;
  - any assignment to `_GIT_HOOKS_DISPATCH`, the dispatcher's re-entry marker.

It replaced gitleaks_guard.sh, which ran gitleaks itself before the command
and had to guess what the commit would contain. Command parsing is
shell_words.py's, so only `git` in command position counts:
`ssh host "... git commit ..."` (another machine's hooks) and `echo` do not
trip it.
`git push`/`git merge --no-verify` are allowed: no hook there scans for
secrets.

A backstop against an accidental bypass, not deliberate evasion: `export
GIT_CONFIG_GLOBAL=...` in an earlier statement, aliases and `eval` are not
seen. Fails open on anything unexpected, saying so on stderr.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shell_words import GitOpt

HOOKS_PATH = "core.hookspath"
# `git commit` options that take the NEXT word as their value.
COMMIT_OPTS_WITH_VALUE = frozenset(
    {
        "-m",
        "-F",
        "-C",
        "-c",
        "-t",
        "--message",
        "--file",
        "--template",
        "--author",
        "--date",
        "--reuse-message",
        "--reedit-message",
        "--fixup",
        "--squash",
        "--cleanup",
        "--trailer",
        "--pathspec-from-file",
    }
)
# Short options whose value is the REST of the cluster; m F C c t also take
# the next word when nothing follows. S and u only ever take an attached value.
SHORT_WITH_VALUE = "mFCctSu"
CONFIG_READS = frozenset({"--get", "--get-all", "--get-regexp", "get", "-l", "--list"})
CONFIG_WRITES = frozenset(
    {"--unset", "--unset-all", "unset", "set", "--add", "--replace-all"}
)
SECTION_WRITES = frozenset(
    {"--remove-section", "--rename-section", "remove-section", "rename-section"}
)
# The dispatcher's re-entry marker (git-hooks/dispatch). It is
# keyed by PID, so a hand-set value should not work anyway; never set it.
DISPATCH_MARKER = "_GIT_HOOKS_DISPATCH"
# Substrings without which a command cannot bypass the hook: the cheap
# pre-filter every Bash call goes through before the parser is imported.
TRIGGERS = ("commit", HOOKS_PATH, "-section", DISPATCH_MARKER.lower())


def _commit_skips_hooks(args: list[str]) -> bool:
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return False
        if a.startswith("--"):
            if len(a) >= len("--no-v") and "--no-verify".startswith(a):
                return True
            i += 2 if a in COMMIT_OPTS_WITH_VALUE else 1
            continue
        if a.startswith("-") and len(a) > 1:
            for k, c in enumerate(a[1:]):
                if c == "n":
                    return True
                if c in SHORT_WITH_VALUE:
                    attached = k + 2 < len(a)
                    i += 1 if attached or c in "Su" else 2
                    break
            else:
                i += 1
            continue
        i += 1
    return False


def _config_writes_hooks_path(args: list[str]) -> bool:
    if any(a in SECTION_WRITES for a in args) and any(
        a.lower() == "core" for a in args
    ):
        return True  # dropping or renaming [core] drops core.hooksPath with it
    key = next((i for i, a in enumerate(args) if a.lower() == HOOKS_PATH), None)
    if key is None or any(a in CONFIG_READS for a in args):
        return False
    if any(a in CONFIG_WRITES for a in args):
        return True
    return any(not a.startswith("-") for a in args[key + 1 :])  # a value


def _global_opts_override(opts: list[GitOpt]) -> bool:
    """`git -c core.hooksPath=...` / `--config-env[=]core.hooksPath=...`."""
    for opt, value in opts:
        if opt.startswith("--config-env="):
            value = opt.split("=", 1)[1]
        elif opt not in ("-c", "--config-env"):
            continue
        if (value or "").lower().startswith(HOOKS_PATH + "="):
            return True
    return False


def bypass_reason(command: str) -> str | None:
    """Why this command would skip the pre-commit hook, or None."""
    from shell_words import assignment, command_words, git_subcommand, segments

    for segment in segments(command):
        for word in segment:
            pair = assignment(word)
            if not pair:
                continue
            if pair[0] == DISPATCH_MARKER:
                return f"{DISPATCH_MARKER} is the hook's own re-entry marker"
            if pair[0].startswith("GIT_CONFIG") and HOOKS_PATH in pair[1].lower():
                return f"{pair[0]} points core.hooksPath elsewhere"

        words = command_words(segment)
        git = git_subcommand(words)
        if git is None:
            continue
        opts, sub, args = git
        if _global_opts_override(opts):
            return "`git -c` / `--config-env` overrides core.hooksPath"
        if sub == "config" and _config_writes_hooks_path(args):
            return "`git config` changes core.hooksPath"
        if sub == "commit":
            if _commit_skips_hooks(args):
                return "`git commit --no-verify` / `-n` skips the hook"
            prefix = segment[: len(segment) - len(words)]
            assigned = {pair[0] for w in prefix if (pair := assignment(w))}
            for var in ("GIT_CONFIG_GLOBAL", "HOME"):
                if var in assigned:
                    return f"{var}=... makes the commit skip the global core.hooksPath"
    return None


def main() -> int:
    try:
        data = json.load(sys.stdin)
        command = (data.get("tool_input") or {}).get("command", "")
        # Every Bash call runs this hook: a substring test first, so the
        # parser is imported only for a command that could matter.
        lowered = command.lower() if isinstance(command, str) else ""
        if not any(t in lowered for t in TRIGGERS):
            return 0
        reason = bypass_reason(command)
    except Exception as e:  # noqa: BLE001 — a backstop must never block every Bash call
        print(f"hook_bypass_guard: {e!r}; command not checked", file=sys.stderr)
        return 0
    if reason is None:
        return 0
    print(
        f"GIT HOOK BYPASS BLOCKED: {reason}.\n"
        "The global pre-commit hook is what scans each commit for secrets.\n"
        "Commit normally; if the hook itself is in the way, ask the user.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
