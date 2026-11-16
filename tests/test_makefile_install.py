"""Tests for `make install` and `make syntax` covering tools/ (mutcheck)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="needs make")


def _make(*args: str, home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", "-s", *args],
        cwd=REPO,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_install_ships_mutcheck_executable(tmp_path: Path) -> None:
    claude_dir = tmp_path / "claude"
    r = _make("install", f"CLAUDE_DIR={claude_dir}", home=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    tool = claude_dir / "tools" / "mutcheck.py"
    assert tool.read_bytes() == (REPO / "tools" / "mutcheck.py").read_bytes()
    assert os.access(tool, os.X_OK)


def test_syntax_scans_tools(tmp_path: Path) -> None:
    r = _make("syntax", home=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ok  tools/mutcheck.py" in r.stdout
