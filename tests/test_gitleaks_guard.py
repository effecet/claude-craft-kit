"""hooks/gitleaks_guard.sh blocks a commit when gitleaks finds a leak."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "gitleaks_guard.sh"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def _run(
    tmp_path: Path, command: str, gitleaks_exit: int | None, cwd: Path
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run the hook in `cwd` with a stub `gitleaks` exiting `gitleaks_exit`
    (None = not installed). Returns the result and the stub's argv log."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "gitleaks.argv"
    # The hook parses its input with python3 and calls git; link both in so
    # dropping a PATH dir that also holds a real gitleaks cannot break them.
    for tool in ("python3", "git"):
        (bin_dir / tool).symlink_to(shutil.which(tool) or f"/usr/bin/{tool}")
    if gitleaks_exit is not None:
        stub = bin_dir / "gitleaks"
        stub.write_text(
            f'#!/bin/sh\necho "$@" > "{argv_log}"\necho "leak report"\nexit {gitleaks_exit}\n'
        )
        stub.chmod(0o755)
    keep = [
        d
        for d in os.environ.get("PATH", "").split(os.pathsep)
        if d and not (Path(d) / "gitleaks").exists()
    ]
    env = {**os.environ, "PATH": os.pathsep.join([str(bin_dir), *keep])}
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(name, None)
    proc = subprocess.run(
        ["bash", str(HOOK)],
        input=json.dumps({"tool_input": {"command": command}}),
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        timeout=30,
    )
    return proc, argv_log


def _args(argv_log: Path) -> list[str]:
    return argv_log.read_text().split()


@pytest.mark.parametrize(
    "command",
    ["git commit -m x", "git -C . commit -m x", "git -c a.b=c commit", "cd sub && git commit"],
)
def test_leak_blocks_the_commit(tmp_path: Path, repo: Path, command: str) -> None:
    proc, argv_log = _run(tmp_path, command, 3, cwd=repo)
    assert proc.returncode == 2
    assert "SECRETS DETECTED" in proc.stderr
    assert "leak report" in proc.stderr
    args = _args(argv_log)
    assert args[:2] == ["git", "--staged"]
    assert "--redact" in args
    assert args[args.index("--exit-code") + 1] == "3"
    assert Path(args[-1]).resolve() == repo.resolve()


@pytest.mark.parametrize("rc", [1, 126])
def test_gitleaks_error_blocks_without_claiming_a_leak(
    tmp_path: Path, repo: Path, rc: int
) -> None:
    proc, _ = _run(tmp_path, "git commit -m x", rc, cwd=repo)
    assert proc.returncode == 2
    assert f"gitleaks failed (rc={rc})" in proc.stderr
    assert "SECRETS DETECTED" not in proc.stderr


def test_clean_scan_allows_the_commit(tmp_path: Path, repo: Path) -> None:
    proc, argv_log = _run(tmp_path, "git commit -m x", 0, cwd=repo)
    assert proc.returncode == 0
    assert proc.stderr == ""
    assert argv_log.exists()  # it scanned, rather than exiting early


def test_subdirectory_scans_the_top_level(tmp_path: Path, repo: Path) -> None:
    # From a subdirectory, scanning "." covers 0 bytes and passes every leak.
    (repo / "sub").mkdir()
    proc, argv_log = _run(tmp_path, "git commit -m x", 3, cwd=repo / "sub")
    assert proc.returncode == 2
    assert Path(_args(argv_log)[-1]).resolve() == repo.resolve()


def test_repo_config_is_picked_up_from_the_top_level(tmp_path: Path, repo: Path) -> None:
    (repo / ".gitleaks.toml").write_text("")
    (repo / "sub").mkdir()
    _, argv_log = _run(tmp_path, "git commit -m x", 0, cwd=repo / "sub")
    args = _args(argv_log)
    config = Path(args[args.index("--config") + 1])
    assert config.resolve() == (repo / ".gitleaks.toml").resolve()


def test_outside_a_repo_warns_and_skips(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    proc, argv_log = _run(tmp_path, "git commit -m x", 3, cwd=plain)
    assert proc.returncode == 0
    assert "not inside a git repo" in proc.stderr
    assert not argv_log.exists()


def test_long_command_is_still_scanned(tmp_path: Path, repo: Path) -> None:
    # `echo | grep -q` under pipefail read a match as a miss past ~64KB.
    proc, _ = _run(tmp_path, "git commit -m x\n" + "y" * 300_000, 3, cwd=repo)
    assert proc.returncode == 2


@pytest.mark.parametrize("command", ["git status", "echo commit"])
def test_non_commit_command_is_not_scanned(tmp_path: Path, repo: Path, command: str) -> None:
    proc, argv_log = _run(tmp_path, command, 3, cwd=repo)
    assert proc.returncode == 0
    assert not argv_log.exists()


def test_missing_gitleaks_warns_and_allows(tmp_path: Path, repo: Path) -> None:
    proc, _ = _run(tmp_path, "git commit -m x", None, cwd=repo)
    assert proc.returncode == 0
    assert "gitleaks not found" in proc.stderr
