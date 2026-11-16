"""Every hook that writes state.json goes through _state.write_state_atomic.

A hook killed mid-write (track.py's 10s timeout on token_tracker, a closed
terminal) used to be able to leave an empty state.json, which resets the
message count and re-fires the wrap-up prompts. The helper writes a temp file
and renames it over the target, so a failure leaves the old file intact.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS))

import _state  # noqa: E402
import session_start  # noqa: E402
import token_tracker  # noqa: E402
import track  # noqa: E402


def test_writes_json_and_leaves_no_temp(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "state.json"
    _state.write_state_atomic(target, {"user_message_count": 3})
    assert json.loads(target.read_text()) == {"user_message_count": 3}
    assert [p.name for p in target.parent.iterdir()] == ["state.json"]


def test_failed_rename_keeps_the_old_file_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    target.write_text('{"user_message_count": 7}')

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(_state.os, "replace", boom)
    with pytest.raises(OSError):
        _state.write_state_atomic(target, {"user_message_count": 8})
    assert json.loads(target.read_text()) == {"user_message_count": 7}
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


@pytest.mark.parametrize("mod", [track, token_tracker, session_start], ids=lambda m: m.__name__)
def test_module_write_state_goes_through_the_helper(
    mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path] = []
    monkeypatch.setattr(mod, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(mod, "write_state_atomic", lambda p, s: calls.append(p))
    mod.write_state({"x": 1})
    assert calls == [tmp_path / "state.json"]


# Ways a hook could write state.json in place, bypassing the helper. A
# grep, not a proof: an alias (`sf = STATE_FILE`) still gets past it.
_DIRECT_WRITE = re.compile(
    r"""open\(\s*['"]?\$?STATE_FILE['"]?\s*,\s*['"][wa]"""
    r"""|STATE_FILE\.(?:write_text|write_bytes)\("""
    r"""|STATE_FILE\.open\(\s*['"][wa]"""
    r"""|(?:>>?|\btee\b(?:\s+-a)?)\s*"?\$\{?STATE_FILE"""
)


@pytest.mark.parametrize(
    "line",
    [
        "with open(STATE_FILE, 'w') as f:",
        "with open('$STATE_FILE', 'w') as f:",
        "STATE_FILE.write_text(data)",
        "STATE_FILE.write_bytes(data)",
        'with STATE_FILE.open("w") as f:',
        'echo "{}" > "$STATE_FILE"',
        'jq . x | tee "$STATE_FILE"',
        "cat x >> ${STATE_FILE}",
    ],
)
def test_guard_catches_each_in_place_form(line: str) -> None:
    assert _DIRECT_WRITE.search(line)


def test_guard_allows_reads() -> None:
    assert not _DIRECT_WRITE.search("with open(STATE_FILE) as f:")
    assert not _DIRECT_WRITE.search('json.load(open("$STATE_FILE"))')


def test_failed_temp_write_keeps_the_old_file_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    target.write_text('{"user_message_count": 7}')
    real = Path.write_text

    def half_write(self: Path, data: str, *a, **k) -> int:
        real(self, data[:5])  # a partial temp file, then the failure
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", half_write)
    with pytest.raises(OSError):
        _state.write_state_atomic(target, {"user_message_count": 8})
    monkeypatch.undo()
    assert json.loads(target.read_text()) == {"user_message_count": 7}
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_stop_gate_survives_an_old_state_module(tmp_path: Path) -> None:
    # Best-effort hook: without write_state_atomic in _state it falls back,
    # and the fallback still writes the state.
    (tmp_path / "_state.py").write_text("def make_fresh_state(preserved=None):\n    return {}\n")
    target = tmp_path / "state.json"
    code = (
        "import sys, stop_gate; from pathlib import Path; "
        "stop_gate.write_state_atomic(Path(sys.argv[1]), {'stop_gate_prompted': True}); "
        "print(stop_gate.write_state_atomic.__module__)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code, str(target)],
        cwd=tmp_path,
        env={"PYTHONPATH": os.pathsep.join([str(tmp_path), str(HOOKS)]), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "stop_gate"
    assert json.loads(target.read_text()) == {"stop_gate_prompted": True}


@pytest.mark.parametrize(
    "path",
    # Hooks are flat in this dir today; a writer in a subdirectory is not scanned.
    sorted(p for p in HOOKS.iterdir() if p.suffix in {".py", ".sh"} and p.name != "_state.py"),
    ids=lambda p: p.name,
)
def test_no_hook_writes_state_json_in_place(path: Path) -> None:
    assert not _DIRECT_WRITE.search(path.read_text()), (
        f"{path.name} writes STATE_FILE in place; use _state.write_state_atomic"
    )


def test_stop_gate_and_on_idle_call_the_helper() -> None:
    # Atomicity is invisible end to end, so pin the call itself.
    assert "write_state_atomic(STATE_FILE, state)" in (HOOKS / "stop_gate.py").read_text()
    assert "write_state_atomic(path, s)" in (HOOKS / "on_idle.sh").read_text()


def test_on_idle_marks_idle_prompted_through_the_helper(tmp_path: Path) -> None:
    session = tmp_path / ".claude" / "session"
    session.mkdir(parents=True)
    state = session / "state.json"
    state.write_text(json.dumps({"user_message_count": 3, "wrap_up_ran": False}))
    # The hook calls `python3`: pin it to this interpreter, or a Homebrew
    # python3.12 run falls through to /usr/bin/python3 (3.9, no datetime.UTC).
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "python3").symlink_to(sys.executable)
    env = {
        "HOME": str(tmp_path),
        # No notifier on this PATH, so that step no-ops.
        "PATH": f"{bindir}:/usr/bin:/bin",
    }
    proc = subprocess.run(
        ["bash", str(HOOKS / "on_idle.sh")], env=env, capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    assert "State: idle_prompted=true" in proc.stdout
    saved = json.loads(state.read_text())
    assert saved["idle_prompted"] is True
    assert saved["user_message_count"] == 3
    assert not list(session.glob("state.json.*.tmp"))


def test_on_idle_without_state_module_warns_and_leaves_state(tmp_path: Path) -> None:
    # A hand-copied on_idle.sh with no _state.py beside it: the hook still
    # exits 0 and logs a warning, and state.json is left as it was.
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    shutil.copy(HOOKS / "on_idle.sh", hooks / "on_idle.sh")
    session = tmp_path / ".claude" / "session"
    session.mkdir(parents=True)
    state = session / "state.json"
    before = json.dumps({"user_message_count": 3, "wrap_up_ran": False})
    state.write_text(before)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "python3").symlink_to(sys.executable)
    proc = subprocess.run(
        ["bash", str(hooks / "on_idle.sh")],
        env={"HOME": str(tmp_path), "PATH": f"{bindir}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Warning: could not update state" in proc.stdout
    assert state.read_text() == before
