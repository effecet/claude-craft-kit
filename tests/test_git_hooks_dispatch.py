"""Tests for git-hooks/dispatch, the global git hook dispatcher.

`make install-git-hooks` points the global `core.hooksPath` at that directory.
Every hook name there is a symlink to `dispatch`: on `pre-commit` it runs
gitleaks over the exact index being committed, and for every hook it then
chains to the repo's own `.git/hooks/<name>`, which a global hooksPath would
otherwise silently disable.

It replaces gitleaks_guard.sh, a Claude Code PreToolUse hook that had to guess
what a Bash command would commit. These tests make real commits in throwaway
repos and assert the harm ("a secret was committed", "the repo's own hook did
not run"). The fake secret is assembled at runtime so this file never trips a
scanner itself.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[1] / "git-hooks"
DISPATCH = HOOKS / "dispatch"

# Client-side and server-side hook names from githooks(5). A name missing here
# means a repo's own hook of that name silently stops running.
HOOK_NAMES = (
    "applypatch-msg",
    "pre-applypatch",
    "post-applypatch",
    "pre-commit",
    "pre-merge-commit",
    "prepare-commit-msg",
    "commit-msg",
    "post-commit",
    "pre-rebase",
    "post-checkout",
    "post-merge",
    "pre-push",
    "pre-receive",
    "update",
    "post-receive",
    "post-update",
    "reference-transaction",
    "pre-auto-gc",
    "post-rewrite",
    "sendemail-validate",
    "post-index-change",
    "p4-changelist",
    "p4-prepare-changelist",
    "p4-post-changelist",
    "p4-pre-submit",
)
# A hook whose mere PRESENCE changes what git does must never be linked: an
# always-present dispatcher would switch that behaviour on in every repo.
PRESENCE_SENSITIVE = ("push-to-checkout", "proc-receive", "fsmonitor-watchman")

GITLEAKS = shutil.which("gitleaks")
needs_gitleaks = pytest.mark.skipif(
    GITLEAKS is None and os.environ.get("CI") is None,
    reason="needs gitleaks (CI installs it, so CI never skips)",
)

# An AWS-style access key id: matched by gitleaks' default aws-access-token rule.
FAKE_KEY = "AKIA" + "Z7QW3RTY5UIO2PLK"


def _env(**extra: str) -> dict[str, str]:
    # Isolate from the machine's own git config, which after install carries a
    # global core.hooksPath pointing at the very hooks under test.
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        **extra,
    }


def _git(
    repo: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", f"core.hooksPath={HOOKS}", *args],
        capture_output=True,
        text=True,
        env=env or _env(),
    )


def _ok(repo: Path, *args: str) -> None:
    res = _git(repo, *args)
    assert res.returncode == 0, res.stderr


def _run_hook(name: str, cwd: Path, **env: str) -> subprocess.CompletedProcess:
    """Invoke a hook the way git does: by its path, from the work tree."""
    return subprocess.run(
        [str(HOOKS / name)],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=_env(**env),
        timeout=10,
    )


def _stage(repo: Path, name: str = "a.txt", text: str = "x\n") -> None:
    (repo / name).write_text(text)
    _ok(repo, "add", name)


def _stage_secret(repo: Path) -> None:
    _stage(repo, "config.py", f'AWS_ACCESS_KEY_ID = "{FAKE_KEY}"\n')


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(r)], check=True, env=_env())
    return r


@pytest.fixture
def seeded(repo: Path) -> Path:
    _stage(repo, "README.md", "hello\n")
    # The seed is setup, not a test of the hook: skip the scan.
    res = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-qm",
            "init",
        ],
        capture_output=True,
        text=True,
        env=_env(),
    )
    assert res.returncode == 0, res.stderr
    return repo


def _head_count(repo: Path) -> int:
    res = _git(repo, "rev-list", "--count", "HEAD")
    return int(res.stdout.strip()) if res.returncode == 0 else 0


def _local_hook(repo: Path, name: str, body: str) -> Path:
    hook = repo / ".git" / "hooks" / name
    hook.write_text("#!/bin/sh\n" + body)
    hook.chmod(0o755)
    return hook


def _assert_blocked(res: subprocess.CompletedProcess, repo: Path, before: int) -> None:
    assert res.returncode != 0, res.stderr
    assert _head_count(repo) == before, "the commit landed"


def _env_without_gitleaks(
    tmp_path: Path, fake_gitleaks: str | None = None, search_path: str = ""
) -> dict[str, str]:
    """An env whose PATH holds git (and a fake gitleaks, given its body) only."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "git").symlink_to(shutil.which("git"))
    if fake_gitleaks is not None:
        fake = bindir / "gitleaks"
        fake.write_text("#!/bin/sh\n" + fake_gitleaks)
        fake.chmod(0o755)
    return _env(PATH=f"{bindir}:/usr/bin:/bin", GITLEAKS_HOOK_SEARCH_PATH=search_path)


# --- installation shape ------------------------------------------------------


def test_dispatch_is_executable() -> None:
    # git ignores a non-executable hook with only a hint, so a lost mode bit
    # would turn the scan off without failing anything.
    assert os.access(DISPATCH, os.X_OK)


@pytest.mark.parametrize("name", HOOK_NAMES)
def test_every_hook_name_links_to_dispatch(name: str) -> None:
    link = HOOKS / name
    assert link.is_symlink(), (
        f"{name} missing: a repo's own {name} hook would stop running"
    )
    assert os.readlink(link) == "dispatch"


def test_no_stray_files_in_hooks_dir() -> None:
    # Dotfiles are never hooks (and the harness drops `.claude/` in its cwd).
    names = {p.name for p in HOOKS.iterdir() if not p.name.startswith(".")}
    assert names == {"dispatch", *HOOK_NAMES}
    assert not set(PRESENCE_SENSITIVE) & set(HOOK_NAMES)


def test_push_to_non_bare_repo_still_updates_its_work_tree(
    seeded: Path, tmp_path: Path
) -> None:
    # receive.denyCurrentBranch=updateInstead is git's own logic only while no
    # push-to-checkout hook exists; a present one that exits 0 claims the job
    # and the target's work tree silently stays behind.
    target = tmp_path / "target"
    subprocess.run(
        ["git", "clone", "-q", str(seeded), str(target)], check=True, env=_env()
    )
    _ok(target, "config", "receive.denyCurrentBranch", "updateInstead")
    _stage(seeded, "new.txt", "pushed\n")
    subprocess.run(
        [
            "git",
            "-C",
            str(seeded),
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-qm",
            "x",
        ],
        check=True,
        env=_env(),
    )
    branch = _git(seeded, "branch", "--show-current").stdout.strip()
    _ok(seeded, "push", "-q", str(target), f"HEAD:{branch}")
    assert (target / "new.txt").read_text() == "pushed\n"


# --- the secret scan ----------------------------------------------------------


@needs_gitleaks
def test_staged_secret_is_blocked_and_redacted(seeded: Path) -> None:
    _stage_secret(seeded)
    res = _git(seeded, "commit", "-qm", "x")
    _assert_blocked(res, seeded, 1)
    assert "SECRETS DETECTED" in res.stderr
    assert "tell the user" in res.stderr, "the block must not point at a bypass"
    assert "config.py" in res.stderr, "the block does not say where the secret is"
    assert FAKE_KEY not in res.stderr + res.stdout, "the secret was echoed into the log"


@needs_gitleaks
def test_commit_all_scans_tracked_edits(seeded: Path) -> None:
    # `commit -a` stages into a temporary index (GIT_INDEX_FILE=.git/index.lock).
    (seeded / "README.md").write_text(f"key {FAKE_KEY}\n")
    _assert_blocked(_git(seeded, "commit", "-qam", "x"), seeded, 1)


@needs_gitleaks
def test_commit_with_pathspec_scans_the_path(seeded: Path) -> None:
    # `commit <path>` builds yet another temporary index (next-index-*.lock).
    (seeded / "README.md").write_text(f"key {FAKE_KEY}\n")
    _assert_blocked(_git(seeded, "commit", "-qm", "x", "README.md"), seeded, 1)


@needs_gitleaks
def test_root_commit_is_scanned(repo: Path) -> None:
    _stage_secret(repo)
    _assert_blocked(_git(repo, "commit", "-qm", "x"), repo, 0)


@needs_gitleaks
def test_clean_commit_passes(seeded: Path) -> None:
    _stage(seeded, text="nothing to see\n")
    _ok(seeded, "commit", "-qm", "x")
    assert _head_count(seeded) == 2


@needs_gitleaks
def test_empty_commit_passes(seeded: Path) -> None:
    _ok(seeded, "commit", "-q", "--allow-empty", "-m", "x")
    assert _head_count(seeded) == 2


@needs_gitleaks
def test_repo_gitleaks_toml_allowlist_is_honoured(seeded: Path) -> None:
    (seeded / ".gitleaks.toml").write_text(
        '[extend]\nuseDefault = true\n[allowlist]\npaths = ["fixtures/"]\n'
    )
    (seeded / "fixtures").mkdir()
    (seeded / "fixtures" / "fake.py").write_text(f'K = "{FAKE_KEY}"\n')
    _ok(seeded, "add", ".")
    _ok(seeded, "commit", "-qm", "x")


def test_missing_gitleaks_warns_and_allows(seeded: Path, tmp_path: Path) -> None:
    _stage(seeded)
    res = _git(seeded, "commit", "-qm", "x", env=_env_without_gitleaks(tmp_path))
    assert res.returncode == 0, res.stderr
    assert "gitleaks not found" in res.stderr


@needs_gitleaks
def test_fallback_search_path_finds_gitleaks(seeded: Path, tmp_path: Path) -> None:
    # Editors and GUI clients often run git without Homebrew on PATH.
    env = _env_without_gitleaks(
        tmp_path, search_path=f"/nonexistent:{Path(GITLEAKS or '/x').parent}"
    )
    _stage_secret(seeded)
    _assert_blocked(_git(seeded, "commit", "-qm", "x", env=env), seeded, 1)


@pytest.mark.parametrize(
    "body",
    [
        "echo 'scanned nothing'; exit 0\n",  # exit 0 without a clean verdict
        "echo 'boom' >&2; exit 126\n",  # crashed
    ],
    ids=["exit0-no-verdict", "crash"],
)
def test_unclean_scan_blocks(seeded: Path, tmp_path: Path, body: str) -> None:
    _stage(seeded)
    env = _env_without_gitleaks(tmp_path, fake_gitleaks=body)
    _assert_blocked(_git(seeded, "commit", "-qm", "x", env=env), seeded, 1)


def test_gitleaks_too_old_is_not_reported_as_a_secret(
    seeded: Path, tmp_path: Path
) -> None:
    # Before 8.19 there is no `gitleaks git`; cobra exits 1 on an unknown
    # command, the same status as a finding.
    old = """echo 'Error: unknown command "git" for "gitleaks"' >&2; exit 1\n"""
    _stage(seeded)
    res = _git(seeded, "commit", "-qm", "x", env=_env_without_gitleaks(tmp_path, old))
    _assert_blocked(res, seeded, 1)
    assert "too old" in res.stderr
    assert "SECRETS DETECTED" not in res.stderr


# --- chaining to the repo's own hooks ----------------------------------------


@needs_gitleaks
def test_local_pre_commit_runs_after_a_clean_scan(seeded: Path) -> None:
    marker = seeded.parent / "ran"
    _local_hook(seeded, "pre-commit", f'touch "{marker}"\n')
    _stage(seeded)
    _ok(seeded, "commit", "-qm", "x")
    assert marker.exists()


@needs_gitleaks
def test_local_pre_commit_failure_blocks(seeded: Path) -> None:
    _local_hook(seeded, "pre-commit", "exit 3\n")
    _stage(seeded)
    _assert_blocked(_git(seeded, "commit", "-qm", "x"), seeded, 1)


@needs_gitleaks
def test_local_pre_commit_does_not_run_when_scan_blocks(seeded: Path) -> None:
    marker = seeded.parent / "ran"
    _local_hook(seeded, "pre-commit", f'touch "{marker}"\n')
    _stage_secret(seeded)
    _assert_blocked(_git(seeded, "commit", "-qm", "x"), seeded, 1)
    assert not marker.exists()


@needs_gitleaks
def test_local_commit_msg_gets_its_argument(seeded: Path) -> None:
    _local_hook(seeded, "commit-msg", 'grep -q "^ok" "$1"\n')
    _assert_blocked(
        _git(seeded, "commit", "-q", "--allow-empty", "-m", "nope"), seeded, 1
    )
    _ok(seeded, "commit", "-q", "--allow-empty", "-m", "ok fine")


def test_non_executable_local_hook_is_skipped(seeded: Path) -> None:
    hook = _local_hook(seeded, "post-commit", "exit 1\n")
    hook.chmod(0o644)  # git's own rule: a non-executable hook does not run
    res = _run_hook("post-commit", seeded)
    assert res.returncode == 0, res.stderr


def test_local_pre_push_gets_argv_stdin_and_status(
    seeded: Path, tmp_path: Path
) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, env=_env())
    _ok(seeded, "remote", "add", "origin", str(remote))
    log = tmp_path / "prepush.log"
    _local_hook(seeded, "pre-push", f'echo "$1 $2" > "{log}"; cat >> "{log}"; exit 7\n')

    res = _git(seeded, "push", "-q", "origin", "HEAD:refs/heads/main")
    assert res.returncode != 0, "the repo's pre-push exit status was swallowed"
    text = log.read_text()
    assert text.startswith(f"origin {remote}"), text
    assert "refs/heads/main" in text, (
        "stdin (the ref list) did not reach the local hook"
    )


def test_worktree_chains_to_common_hooks_dir(seeded: Path, tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    _local_hook(seeded, "post-commit", f'touch "{marker}"\n')
    wt = tmp_path / "wt"
    _ok(seeded, "worktree", "add", "-q", str(wt))
    res = _run_hook("post-commit", wt)
    assert res.returncode == 0, res.stderr
    assert marker.exists(), "a worktree's hooks live in the common dir"


def test_local_hooks_dir_pointing_at_us_does_not_recurse(seeded: Path) -> None:
    hooks = seeded / ".git" / "hooks"
    shutil.rmtree(hooks)
    hooks.symlink_to(HOOKS)
    res = _run_hook("post-commit", seeded)
    assert res.returncode == 0, res.stderr


def _local_hooks_path_wrapper(repo: Path, hooks_dir: str) -> None:
    """A repo with its OWN core.hooksPath, whose pre-commit execs ours —
    the documented fix, since a local hooksPath bypasses the global one."""
    (repo / hooks_dir).mkdir(exist_ok=True)
    wrapper = repo / hooks_dir / "pre-commit"
    wrapper.write_text(f'#!/bin/sh\nexec "{HOOKS / "pre-commit"}" "$@"\n')
    wrapper.chmod(0o755)


@needs_gitleaks
@pytest.mark.parametrize("hooks_dir", [".githooks", ".git/hooks"])
def test_local_hooks_path_wrapper_scans_once_and_terminates(
    seeded: Path, hooks_dir: str
) -> None:
    # `.git/hooks` is the loop case: we chain to <common>/hooks/pre-commit,
    # which is the wrapper, which execs us again.
    _local_hooks_path_wrapper(seeded, hooks_dir)
    _stage_secret(seeded)
    res = subprocess.run(
        [
            "git",
            "-C",
            str(seeded),
            "-c",
            f"core.hooksPath={hooks_dir}",
            "commit",
            "-qm",
            "x",
        ],
        capture_output=True,
        text=True,
        env=_env(),
        timeout=30,
    )
    _assert_blocked(res, seeded, 1)
    assert res.stderr.count("SECRETS DETECTED") == 1


@needs_gitleaks
def test_local_hooks_path_wrapper_lets_a_clean_commit_through(seeded: Path) -> None:
    _local_hooks_path_wrapper(seeded, ".git/hooks")
    _stage(seeded)
    res = subprocess.run(
        [
            "git",
            "-C",
            str(seeded),
            "-c",
            "core.hooksPath=.git/hooks",
            "commit",
            "-qm",
            "x",
        ],
        capture_output=True,
        text=True,
        env=_env(),
        timeout=30,
    )
    assert res.returncode == 0, res.stderr
    assert _head_count(seeded) == 2


@needs_gitleaks
def test_wrapper_that_calls_without_exec_fails_loudly_instead_of_looping(
    seeded: Path,
) -> None:
    # The wrapper runs us as a CHILD, so every round has a new PID and the
    # marker cannot stop it; the depth cap must. A clean commit is the loop
    # case: a secret blocks before any chaining.
    wrapper = seeded / ".git" / "hooks" / "pre-commit"
    wrapper.write_text(f'#!/bin/sh\n"{HOOKS / "pre-commit"}" "$@"\n')
    wrapper.chmod(0o755)
    _stage(seeded)
    res = subprocess.run(
        [
            "git",
            "-C",
            str(seeded),
            "-c",
            "core.hooksPath=.git/hooks",
            "commit",
            "-qm",
            "x",
        ],
        capture_output=True,
        text=True,
        env=_env(),
        timeout=30,
    )
    _assert_blocked(res, seeded, 1)
    assert "must 'exec' it" in res.stderr


@needs_gitleaks
def test_commit_made_inside_a_repo_hook_is_scanned(
    seeded: Path, tmp_path: Path
) -> None:
    # The marker is inherited by everything the repo hook spawns; it must not
    # let a commit the hook makes (here, in a sibling worktree) skip the scan.
    wt = tmp_path / "wt"
    _ok(seeded, "worktree", "add", "-q", str(wt))
    rc_file = tmp_path / "nested-rc"
    _local_hook(
        seeded,
        "pre-commit",
        "unset GIT_INDEX_FILE GIT_DIR GIT_WORK_TREE\n"
        f'cd "{wt}" && printf \'K = "{FAKE_KEY}"\\n\' > s.py && git add s.py\n'
        f'git commit -qm nested; echo $? > "{rc_file}"\n',
    )
    _stage(seeded)
    _ok(seeded, "commit", "-qm", "outer")
    assert rc_file.read_text().strip() != "0", "the nested secret commit landed"
    assert "s.py" not in _git(wt, "log", "--name-only", "-1").stdout


@needs_gitleaks
@pytest.mark.parametrize(
    "marker",
    [
        "$GD:pre-commit",  # no PID at all
        "$GD:pre-commit:$$",  # the shell's PID, which `exec git` hands to git
    ],
)
def test_hand_set_marker_does_not_skip_the_scan(seeded: Path, marker: str) -> None:
    # The gate's repro: git runs the hook as its CHILD, so git's own PID (the
    # one a caller can know) is the hook's PARENT, never the hook itself.
    gitdir = _git(seeded, "rev-parse", "--absolute-git-dir").stdout.strip()
    _stage_secret(seeded)
    script = (
        f'GD="{gitdir}"; export _GIT_HOOKS_DISPATCH="{marker}"; '
        f'exec git -C "{seeded}" -c core.hooksPath="{HOOKS}" commit -qm x'
    )
    res = subprocess.run(
        ["sh", "-c", script], capture_output=True, text=True, env=_env()
    )
    _assert_blocked(res, seeded, 1)


@needs_gitleaks
def test_marker_matching_this_process_still_scans(seeded: Path) -> None:
    # PIDs are predictable, so assume a caller CAN forge the exact marker: the
    # one thing it may buy is skipping the repo's own hook, never the scan.
    gitdir = _git(seeded, "rev-parse", "--absolute-git-dir").stdout.strip()
    _stage_secret(seeded)
    # exec keeps the PID, so this shell's `$$` IS the dispatcher's PID.
    script = (
        f'export _GIT_HOOKS_DISPATCH="{gitdir}:pre-commit:$$"; '
        f'exec "{HOOKS / "pre-commit"}"'
    )
    res = subprocess.run(
        ["sh", "-c", script], cwd=seeded, capture_output=True, text=True, env=_env()
    )
    assert res.returncode != 0, "a forged exact marker skipped the scan"
    assert "SECRETS DETECTED" in res.stderr


@needs_gitleaks
def test_forged_depth_can_only_block(seeded: Path) -> None:
    _stage(seeded)
    env = _env(_GIT_HOOKS_DEPTH="not-a-number")
    res = _git(seeded, "commit", "-qm", "x", env=env)
    assert res.returncode == 0, res.stderr  # garbage resets to 1
    _stage(seeded, "b.txt")
    _assert_blocked(
        _git(seeded, "commit", "-qm", "y", env=_env(_GIT_HOOKS_DEPTH="99")), seeded, 2
    )


def test_outside_a_repo_is_a_no_op(tmp_path: Path) -> None:
    res = _run_hook(
        "post-checkout", tmp_path, GIT_CEILING_DIRECTORIES=str(tmp_path.parent)
    )
    assert res.returncode == 0, res.stderr
