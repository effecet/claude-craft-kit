"""Tests for hook_bypass_guard.py, the PreToolUse guard on Claude's Bash calls.

The secret scan moved into a global git pre-commit hook (git-hooks/,
see test_git_hooks_dispatch.py). What is left for a PreToolUse hook is to stop
Claude's own commands from switching that hook off: `--no-verify`, or pointing
core.hooksPath somewhere else. It targets accidental bypass, not an adversary.

The guard it replaced also blocked anything that merely MENTIONED a commit —
`ssh host "... git commit ..."` included. Only `git` in command position
counts now, so the allow table pins those cases too.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parents[1] / "hooks"
HOOK = HOOKS_DIR / "hook_bypass_guard.py"
sys.path.insert(0, str(HOOKS_DIR))

from hook_bypass_guard import bypass_reason  # noqa: E402


def _run_raw(stdin: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK)], input=stdin, text=True, capture_output=True
    )


def _run(command: str) -> subprocess.CompletedProcess:
    return _run_raw(json.dumps({"tool_input": {"command": command}, "cwd": "/tmp"}))


BLOCKED = [
    'git commit --no-verify -m "x"',
    'git commit -n -m "x"',
    'git commit -anm "x"',
    'git commit -qn -m "x"',
    "git commit -mfix -n",  # the attached value ends at the word, -n is a flag
    'git commit --no-verif -m "x"',  # git accepts unambiguous abbreviations
    'git commit --no-v -m "x"',
    'git add . && git commit --no-verify -m "x"',
    'cd sub; git commit -n -m "x"',
    'git -C sub commit -n -m "x"',
    'git -c core.hooksPath=/dev/null commit -m "x"',
    'git -c CORE.HOOKSPATH=/tmp/none commit -m "x"',
    "git -c core.hooksPath= push",
    'git --config-env=core.hooksPath=EMPTY commit -m "x"',
    'git --config-env core.hooksPath=EMPTY commit -m "x"',
    "git config core.hooksPath /tmp/none",
    "git config --global core.hooksPath /tmp/none",
    "git config --unset core.hooksPath",
    "git config --global --unset-all core.hooksPath",
    "git config set core.hooksPath /tmp/none",
    "git config unset --global core.hooksPath",
    "git config --global --remove-section core",
    "git config --global --rename-section core old-core",
    "git config remove-section --global core",
    "GIT_CONFIG_PARAMETERS=\"'core.hooksPath=/x'\" git commit -m x",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/x git commit -m x",
    "export GIT_CONFIG_KEY_0=core.hooksPath",
    "GIT_CONFIG_GLOBAL=/dev/null git commit -m x",
    "HOME=/tmp git commit -m x",
    '_GIT_HOOKS_DISPATCH="$PWD/.git:pre-commit:1" git commit -m x',
    "export _GIT_HOOKS_DISPATCH=x",
    'env GIT_CONFIG_GLOBAL=/dev/null git commit -m "x"',
    'bash -c "git commit --no-verify -m x"',
    "sh -c 'cd sub && git commit -n -m x'",
    '/usr/bin/git commit --no-verify -m "x"',
    'command git commit -n -m "x"',
    '(git commit --no-verify -m "x")',
]

ALLOWED = [
    'git commit -m "x"',
    'git commit -am "x"',
    'git commit -m "do not use --no-verify here"',
    'git commit -m "-n is a flag"',
    "git commit -m-n",  # -m with an attached value
    "git commit -m x -- -n",  # after --, a path named -n
    'git commit --message "--no-verify" -q',
    "git commit -F msg.txt",
    "git commit -C HEAD~1 --amend",  # -C takes a value; n is not in the cluster
    'git commit -uno -m "x"',  # -u takes an attached value ("no")
    'git commit --verbose -m "x"',
    "git commit --no-edit --amend",
    'ssh host "cd ~/x && git commit --no-verify -m y"',  # another machine's hooks
    "echo git commit --no-verify",
    'grep -n "git commit -n" notes.md',
    "git config --get core.hooksPath",
    "git config core.hooksPath",  # a read
    "git config --global --get core.hooksPath",
    "git config --get-all core.hooksPath '^/tmp'",  # a value-regex, not a value
    "git config get core.hooksPath",
    "git config --list",
    "git config --remove-section alias",  # not [core]
    "git commit -m x  # --no-verify would be wrong here",
    "git commit -F - <<'EOF'\nsubject\n\n--no-verify is not a flag here\nEOF",
    "git commit -m \"$(cat <<'EOF'\nsubject -n --no-verify\nEOF\n)\"",
    "git config --global --unset user.name",  # writes, but not core.hooksPath
    "git config user.name t",
    'git log --grep "core.hooksPath"',
    "GIT_CONFIG_GLOBAL=/dev/null git status",
    "HOME=/tmp git log -1",
    "git push --no-verify origin main",  # out of scope: no secret scan on push
    "git merge --no-verify feature",
    "ls -la",
    "",
]


@pytest.mark.parametrize("command", BLOCKED)
def test_blocks_bypass(command: str) -> None:
    assert bypass_reason(command), f"not blocked: {command!r}"


@pytest.mark.parametrize("command", ALLOWED)
def test_allows_ordinary_commands(command: str) -> None:
    assert bypass_reason(command) is None, f"blocked: {command!r}"


def test_unbalanced_quotes_still_checked() -> None:
    assert bypass_reason('git commit --no-verify -m "unterminated')


# --- main(): the wiring Claude Code actually runs ----------------------------


def test_main_blocks_with_exit_2_and_explains() -> None:
    res = _run('git commit --no-verify -m "x"')
    assert res.returncode == 2, res.stderr
    assert "secret" in res.stderr.lower()


def test_main_allows_with_exit_0() -> None:
    res = _run('git commit -m "x"')
    assert res.returncode == 0, res.stderr


def test_main_mixed_case_hookspath_reaches_the_parser() -> None:
    # The pre-filter is a substring test; it must not be case-sensitive.
    assert _run("git -c CORE.HOOKSPATH=/x status").returncode == 2


def test_main_marker_export_reaches_the_parser() -> None:
    # No "commit" in it, yet it primes a later commit to skip the scan.
    assert _run("export _GIT_HOOKS_DISPATCH=x").returncode == 2


def test_main_section_removal_reaches_the_parser() -> None:
    # No "commit" and no "core.hooksPath" in it, yet it drops the setting.
    assert _run("git config --global --remove-section core").returncode == 2


def test_malformed_payload_fails_open() -> None:
    # A broken payload must never block every Bash call; the git hook is the
    # real control, this guard only a second layer.
    assert _run_raw("not json").returncode == 0
