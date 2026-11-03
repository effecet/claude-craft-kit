#!/usr/bin/env python3
"""
ruff_workflow_guard.py

PreToolUse:Bash hook — blocks `git commit` (exit 2) when CWD is a
workflow-bearing repo with ruff config and ruff finds issues.

Scope is what the commit can contain: the files git tracks, plus the
untracked files the same command is about to `git add` (the hook runs
BEFORE the command, so `git add new.py && git commit` still sees
new.py as untracked). CI lints a clean checkout, so other untracked
files (harness-synced skills, deployed script copies) cannot fail there
and must not block here. Which of those files are Python is left to
ruff's own include/exclude, exactly as CI's `ruff check .` decides it.
Ruff reads working-tree contents, not the index — an approximate mirror.

Which repo: the one the commit lands in. The command is parsed by
shell_words.py from the session CWD (the hook payload's `cwd`), following
`cd`/`pushd`, subshells and `git -C`, and the result is resolved to its
work-tree root, where the workflow dir and pyproject.toml live. Forms that
cannot be read (`cd $VAR`, `cd -P`, `popd`, a `cd` inside `bash -c`) fall
back to the session CWD and lint every untracked file — so when that CWD
is not inside the target repo, such a commit is not checked at all (the
target is unknowable; the guard before this one had the same blind spot).

Why structural enforcement:
"Run the CI mirror before pushing" is easy to skip under time pressure.
A pre-commit gate catches `ruff check` and `ruff format` failures locally
instead of post-push CI.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from types import EllipsisType
from pathlib import Path

GIT_COMMIT_RE = re.compile(
    r"(^|[\s;&|(\"'])git\s+(?:[^;&|\n]*?\s+)?commit(?=[^\w-]|$)"
)
WORKFLOW_DIRS = (".forgejo/workflows", ".github/workflows")
# A `git add` the lexer cannot see (e.g. inside `bash -c '...'`).
GIT_ADD_RE = re.compile(r"\bgit\s+(?:-\S+\s+)*(?:add|stage)\b")
REDIRECTS = frozenset({">", ">>", "<", ">&", "<&", "&>", ">|"})
# A redirect glued to its fd or target (`2>&1`, `>log`, `&>log`).
REDIRECT_RE = re.compile(r"^(?:\d+|&)?[<>]")
QUOTES = frozenset("\"'")
# Anywhere in the command, not only first word: `builtin cd`, `command cd`
# and `CDPATH=. cd` all move the directory `git add` resolves paths from.
DIR_CHANGERS = frozenset({"cd", "pushd", "popd"})
DIR_CHANGE_RE = re.compile(r"(?:^|[\s;&|(\'\"])(?:cd|pushd|popd)(?:\s|$)")
SHELL_C_RE = re.compile(r"\b(?:ba|z|da)?sh\s+(?:-\w+\s+)*-\w*c\b")
# A path the shell rewrites before git sees it: `$F`, `$(...)`, backticks,
# braces, `~`. Its real value is unknowable here.
SHELL_EXPANSION_CHARS = frozenset("$`{~")


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


def _add_args(args: list[str]) -> tuple[list[str], bool] | None:
    """(pathspecs, adds-everything) for one `git add` argv, None if unreadable."""
    add_all = False
    only_paths = False
    paths: list[str] = []
    for k, arg in enumerate(args):
        if arg in REDIRECTS or REDIRECT_RE.match(arg):
            break  # `>`, `2>&1`, `&>log`: shell_words keeps them one word
        if arg.isdigit() and k + 1 < len(args) and args[k + 1] in REDIRECTS:
            break  # the fd of `2 > log`
        if only_paths or not arg.startswith("-"):
            if (SHELL_EXPANSION_CHARS | QUOTES) & set(arg):
                return None  # expanded by the shell, or an unbalanced quote
            paths.append(arg)
        elif arg == "--":
            only_paths = True
        elif arg in ("--all", "--no-ignore-removal") or (
            not arg.startswith("--") and "A" in arg
        ):
            add_all = True
        elif arg.startswith("--pathspec-from-file"):
            return None
    return paths, add_all


def _dir_target(target: str, cwd: Path, command: str) -> Path | None:
    """A `cd` / `git -C` directory, or None when the shell's rewrite of it is
    unknowable. A leading bare `~` or `~/` is the home dir,
    unless a `~` is quoted or backslash-escaped anywhere in the command
    (shell_words strips both, and the shell keeps such a `~` literal), or the
    command assigns HOME (the shell would expand against that, not ours).
    `~user` and `$`/backtick/brace stay unknowable."""
    if target == "~" or target.startswith("~/"):
        if (
            re.search(r"[\"'\\]~", command)
            or re.search(r"(?:^|[\s;&|(])HOME=", command)
            or SHELL_EXPANSION_CHARS & set(target[1:])
        ):
            return None
        return Path(target).expanduser().resolve()
    if SHELL_EXPANSION_CHARS & set(target):
        return None
    return (cwd / target).resolve()


def _cd_target(words: list[str], segment: list[str], cwd: Path, command: str) -> Path | None:
    """Where a `cd`/`pushd` lands, or None when that cannot be known here."""
    target = words[1] if len(words) > 1 else None
    prefix = segment[: len(segment) - len(words)]
    if (
        words[0] == "popd"
        or target is None
        or target.startswith("-")  # `-`, `-P`, `-L`, `--`
        or any(w.startswith("CDPATH=") for w in prefix)
    ):
        return None
    return _dir_target(target, cwd, command)


def _commit_plan(command: str, start: Path) -> tuple[Path, list[str] | None]:
    """(directory the commit runs in, what its `git add` stages from untracked).

    The pathspecs are relative to that directory, ready for
    `git ls-files --others`. None: no untracked file gets added; [] means
    every untracked file. The command is parsed by shell_words.py (shared
    with the other Bash guards), following `cd`/`pushd`, subshells and
    `git -C`, so `cd sub && git add x.py` stages `sub/x.py`, not everything.
    A form this cannot read (`cd $VAR`, `cd -P`, `popd`, `CDPATH=`, a `cd`
    inside a `bash -c` body, `--git-dir`, `--pathspec-from-file`, a
    `$`/`~`/glob-brace/quoted path) resolves to (start, []) — linting too
    much is safe, too little lets a CI failure through. A bare leading `~` in
    a cd / -C target is the one expansion resolved (see _dir_target).
    """
    from shell_words import command_words, git_subcommand, segments

    strict: tuple[Path, list[str] | None] = (start, [])
    # shell_words inlines `bash -c` bodies, so a `cd` there would leak into
    # the outer command's directory; that body's scope is unknowable here.
    if SHELL_C_RE.search(command) and DIR_CHANGE_RE.search(command):
        return strict
    cwd, stack = start, []
    commit_dir: Path | None = None
    adds: list[tuple[Path, list[str], bool]] = []
    found_add = False
    for segment in segments(command):
        if segment == ["("]:
            stack.append(cwd)
            continue
        if segment == [")"]:
            cwd = stack.pop() if stack else cwd
            continue
        words = command_words(segment)
        if not words:
            continue
        if words[0] in DIR_CHANGERS:
            landed = _cd_target(words, segment, cwd, command)
            if landed is None:
                return strict
            cwd = landed
            continue
        git = git_subcommand(words)
        if git is None:
            continue
        opts, sub, args = git
        repo = cwd
        for opt, value in opts:
            if opt in ("--git-dir", "--work-tree") or opt.startswith(
                ("--git-dir=", "--work-tree=")
            ):
                return strict  # stages in another tree
            if opt == "-C" and value is not None:
                landed = _dir_target(value, repo, command)
                if landed is None:
                    return strict
                repo = landed
        if sub == "commit":
            commit_dir = repo
        elif sub in ("add", "stage"):
            found_add = True
            parsed = _add_args(args)
            if parsed is None:
                return strict
            adds.append((repo, *parsed))

    target_dir = commit_dir or start
    if not found_add:
        # A `git add` the parser cannot see (e.g. inside `eval`).
        return (target_dir, []) if GIT_ADD_RE.search(command) else (target_dir, None)
    specs: list[str] = []
    for add_dir, paths, add_all in adds:
        if not paths and add_all:
            return (target_dir, [])
        for p in paths:
            if add_dir == target_dir:
                specs.append(p)
                continue
            rel = os.path.relpath(add_dir / p, target_dir)
            if rel == ".." or rel.startswith("../"):
                return (target_dir, [])  # staged outside where it commits
            specs.append(rel)
    return (target_dir, specs) if specs else (target_dir, None)


def _git_add_pathspecs(command: str, start: Path) -> list[str] | None:
    """What the command's `git add` stages, relative to where it commits."""
    return _commit_plan(command, start)[1]


def _toplevel(path: Path) -> Path | None:
    """The work-tree root containing `path`, or None outside a work tree."""
    try:
        res = subprocess.run(
            [
                "git", "-c", "core.fsmonitor=false", "-C", str(path),
                "rev-parse", "--show-toplevel",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return Path(res.stdout.strip()) if res.returncode == 0 else None


def _commit_target(command: str, start: Path) -> tuple[Path, list[str] | None] | None:
    """(repo root the commit lands in, pathspecs relative to it), or None.

    Workflows and pyproject.toml live at the root, so a commit run from a
    subdirectory (`cd sub && git commit`, `git -C sub commit`, or a session
    whose CWD is `sub`) is checked at the root, with its pathspecs rebased.
    """
    commit_dir, specs = _commit_plan(command, start)
    top = _toplevel(commit_dir)
    if top is None:
        return None
    if specs and commit_dir.resolve() != top.resolve():
        # Both sides resolved: a symlinked CWD (~/.claude/hooks, /tmp) must
        # not turn the rebase into `../..` paths outside the repo.
        real_dir, real_top = commit_dir.resolve(), top.resolve()
        specs = [os.path.relpath(real_dir / s, real_top) for s in specs]
    return top, specs


def _git_files(cwd: Path, args: list[str]) -> list[str] | None:
    """`git <args> -z` names present on disk, or None if git can't say.

    A tracked file deleted from the working tree is skipped: passed to
    ruff, a missing path is an error that would block the commit. Names
    are decoded like the filesystem does, so a non-UTF-8 name can't crash
    the hook.
    """
    try:
        proc = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", *args],
            cwd=cwd,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    names = (os.fsdecode(n) for n in proc.stdout.split(b"\0") if n)
    return [n for n in names if (cwd / n).is_file()]


def _files_in_commit_scope(
    cwd: Path, command: str, specs: list[str] | None | EllipsisType = ...
) -> list[str] | None:
    """Tracked files plus the untracked ones `command` will `git add`.

    `specs` are pathspecs already resolved relative to `cwd` (main() passes
    them, so the command is parsed once); left out, `command` is parsed here.
    """
    files = _git_files(cwd, ["ls-files", "-z"])
    if specs is ...:
        specs = _git_add_pathspecs(command, cwd)
    if files is None or specs is None:
        return files
    added = _git_files(
        cwd, ["ls-files", "-z", "--others", "--exclude-standard", "--", *specs]
    )
    if added is None:
        return None
    return sorted(set(files) | set(added))


def _ruff_discovered_files(cwd: Path) -> set[str] | None:
    """Files `ruff check .` would lint — CI's exact set — relative to cwd.

    Ruff's own discovery applies include, exclude and .gitignore. Handing
    ruff every tracked file instead would pass it `.md` files, which
    `ruff format` rejects rather than skips.
    """
    try:
        proc = subprocess.run(
            ["ruff", "check", "--show-files", "."],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    root = cwd.resolve()
    found: set[str] = set()
    for line in proc.stdout.splitlines():
        try:
            found.add(Path(line.strip()).relative_to(root).as_posix())
        except ValueError:
            continue
    return found


def _run_ruff(
    cwd: Path, command: str = "", specs: list[str] | None | EllipsisType = ...
) -> tuple[int, str]:
    """Run ruff check + ruff format --check. Return (exit_code, combined_output).

    Lints the files CI would lint that are also in the commit's scope.
    `--force-exclude` keeps every exclude setting in force for explicit paths,
    and `--` stops a file named like an option (`--config=...`) from being
    read as one. If either list is unavailable, falls back to the whole
    tree and says so.

    Returns (0, "") on success. Returns (nonzero, output) on ruff failure.
    On infra errors (OSError — incl. a missing ruff or an argv too long for
    a huge repo — or TimeoutExpired) logs to stderr and returns (0, "") —
    fail-open, but loudly (a silent guard is a useless guard).
    """
    scope = _files_in_commit_scope(cwd, command, specs)
    discovered = _ruff_discovered_files(cwd) if scope is not None else None
    files = None if discovered is None else sorted(discovered & set(scope or ()))
    if files is None:
        print(
            "ruff_workflow_guard: could not list files; linting the whole tree",
            file=sys.stderr,
        )
        targets, shown = ["."], "."
    elif not files:
        print("ruff_workflow_guard: no files in commit scope", file=sys.stderr)
        return 0, ""
    else:
        targets = ["--force-exclude", "--", *files]
        shown = f"--force-exclude -- <{len(files)} files in commit scope>"

    for base in (["ruff", "check"], ["ruff", "format", "--check"]):
        try:
            proc = subprocess.run(
                [*base, *targets],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"ruff_workflow_guard: ruff invocation failed: {e}", file=sys.stderr)
            return 0, ""
        if proc.returncode != 0:
            parts = [f"$ {' '.join(base)} {shown}"]
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

    # The repo the commit lands in, from the session's CWD (the payload's,
    # which follows earlier `cd`s): `cd other && git commit`, `git -C sub
    # commit` and a session sitting in a subdirectory are all checked at
    # that repo's root.
    target = _commit_target(command, Path(data.get("cwd") or Path.cwd()))
    if target is None:
        return 0
    cwd, specs = target
    if not _has_workflow_dir(cwd):
        return 0
    if not _has_ruff_config(cwd):
        return 0
    if shutil.which("ruff") is None:
        return 0

    code, output = _run_ruff(cwd, command, specs)
    if code == 0:
        return 0

    print(
        f"RUFF PRE-COMMIT GATE FAILED in {cwd}\n\n"
        f"{output}\n\n"
        f"Workflow-bearing repo detected ({', '.join(WORKFLOW_DIRS)}); "
        f"the same ruff run will fail in CI. Fix the files named above with:\n"
        f"  ruff check --fix -- <file>...\n"
        f"  ruff format -- <file>...\n"
        f"then re-stage and re-commit. Bypass with --no-verify is NOT honored "
        f"(this is a Claude harness PreToolUse hook, not a git pre-commit).",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
