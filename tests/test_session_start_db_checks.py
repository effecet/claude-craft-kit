"""Tests for session_start._run_db_checks (the three backend-bound checks run
concurrently, so an unreachable backend costs the slowest timeout rather than
the sum of all three) and _run_in_own_group (brain_check's timeout kills the
whole process group, not just `make`)."""

from __future__ import annotations

import inspect
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

import session_start as ss


def _stub(monkeypatch, *, brain=None, orphans=None, pending=None):
    monkeypatch.setattr(ss, "brain_check", brain or (lambda: ["brain"]))
    monkeypatch.setattr(ss, "orphan_drift", orphans or (lambda: ["orphan"]))
    monkeypatch.setattr(ss, "pending_section", pending or (lambda: "## Pending\nx\n"))


def test_returns_each_checks_result_under_its_name(monkeypatch):
    _stub(monkeypatch)
    db = ss._run_db_checks()
    assert db.brain == ["brain"]
    assert db.orphans == ["orphan"]
    assert db.pending == "## Pending\nx\n"


def test_the_three_checks_overlap(monkeypatch):
    """A barrier only releases when all three are in flight at once. Run in
    sequence, the first waiter breaks it, every check falls back, and the
    equality below fails: no wall-clock assertion, so a loaded runner cannot
    flake it."""
    barrier = threading.Barrier(3, timeout=5)

    def waits(value):
        def _check():
            barrier.wait()
            return value

        return _check

    _stub(
        monkeypatch,
        brain=waits(["b"]),
        orphans=waits(["o"]),
        pending=waits("## Pending\np\n"),
    )
    assert ss._run_db_checks() == (["b"], ["o"], "## Pending\np\n")


def _boom():
    raise RuntimeError("secret-bearing message")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("brain", ["🧠 Brain: ⚠️ check failed — RuntimeError"]),
        ("orphans", []),
        ("pending", ss.PENDING_UNAVAILABLE),
    ],
)
def test_one_raising_check_falls_back_and_spares_the_others(
    monkeypatch, capsys, name, expected
):
    _stub(monkeypatch, **{name: _boom})
    db = ss._run_db_checks()
    assert getattr(db, name) == expected
    healthy = {"brain": ["brain"], "orphans": ["orphan"], "pending": "## Pending\nx\n"}
    for other, value in healthy.items():
        if other != name:
            assert getattr(db, other) == value
    err = capsys.readouterr().err
    assert f"{name} check failed: RuntimeError" in err
    # Exception TYPE only: some exception strings embed argv / credentials.
    assert "secret-bearing" not in err


def test_pending_fallback_is_not_an_empty_queue():
    assert "None." not in ss.PENDING_UNAVAILABLE


class TestMainWiring:
    """main() is too side-effectful to call in a unit test (state file, brief
    file, backend sync), so these pin its wiring at the source level."""

    src = inspect.getsource(ss.main)

    def test_main_runs_the_db_checks_once_after_memory_decay(self):
        assert self.src.count("_run_db_checks()") == 1
        assert self.src.index("memory_decay(") < self.src.index("_run_db_checks()")

    def test_main_does_not_call_the_checks_directly(self):
        for fn in ("brain_check()", "orphan_drift()", "pending_section()"):
            assert fn not in self.src

    def test_main_routes_each_result_to_its_own_group(self):
        assert "brain=db.brain" in self.src
        assert "orphans=db.orphans" in self.src


class TestBuildBriefPending:
    @pytest.fixture(autouse=True)
    def _quiet_collectors(self, monkeypatch):
        """No git, spec_audit subprocess or memory-dir scan from these tests."""
        monkeypatch.setattr(ss, "git_summary", lambda: ["- clean"])
        monkeypatch.setattr(ss, "_open_specs_summary", lambda: "")
        monkeypatch.setattr(ss, "stale_project_memories", lambda: [])

    def test_uses_precomputed_pending_without_querying(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            ss, "pending_section", lambda: calls.append(1) or "## Pending\nq\n"
        )
        brief = ss.build_brief({}, False, pending="## Pending\nPRECOMPUTED\n")
        assert "PRECOMPUTED" in brief
        assert calls == []

    def test_still_queries_when_not_given_pending(self, monkeypatch):
        monkeypatch.setattr(ss, "pending_section", lambda: "## Pending\nLIVE\n")
        assert "LIVE" in ss.build_brief({}, False)


def _parent_script(pid_file, *, own_session: bool) -> str:
    """A parent that spawns a long-lived grandchild holding the inherited
    stdout/stderr (the shape of `make` running a node recipe), records the
    grandchild's pid, then hangs. own_session=True makes the grandchild leave
    the process group, as a daemonising child would."""
    return (
        "import subprocess, sys, time\n"
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],"
        f" start_new_session={own_session})\n"
        f"open({str(pid_file)!r}, 'w').write(str(c.pid))\n"
        "time.sleep(30)\n"
    )


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
def grandchild(tmp_path):
    """Path the grandchild's pid is written to; kills it at teardown."""
    pid_file = tmp_path / "grandchild.pid"
    yield pid_file
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text()), signal.SIGKILL)
        except (ProcessLookupError, ValueError):
            pass


def test_timeout_kills_the_whole_group_not_just_the_parent(grandchild):
    """subprocess.run kills only the parent; the group kill must take the
    grandchild with it. Asserted on the grandchild itself, not on timing."""
    with pytest.raises(subprocess.TimeoutExpired):
        # 3s: the parent must have spawned the grandchild before the kill.
        ss._run_in_own_group(
            [sys.executable, "-c", _parent_script(grandchild, own_session=False)], 3
        )
    pid = int(grandchild.read_text())
    deadline = time.monotonic() + 5
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)  # an orphan is reaped by init asynchronously
    assert not _alive(pid)


def test_drain_after_the_kill_is_bounded(grandchild):
    """A grandchild that left the group survives the kill and keeps the pipes
    open; the drain must give up instead of waiting the 30s it lives."""
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        ss._run_in_own_group(
            [sys.executable, "-c", _parent_script(grandchild, own_session=True)], 3
        )
    assert time.monotonic() - start < 10


def test_run_in_own_group_returns_output_and_code():
    r = ss._run_in_own_group(
        [
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)",
        ],
        10,
    )
    assert (r.returncode, r.stdout, r.stderr) == (3, "out\n", "err\n")


def test_brain_check_reports_a_timeout(monkeypatch, tmp_path):
    (tmp_path / "Makefile").write_text("")
    monkeypatch.setattr(ss, "BRAIN_ENABLED", True)
    monkeypatch.setattr(ss, "MEMORY_REPO_DIR", tmp_path)

    def times_out(cmd, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(ss, "_run_in_own_group", times_out)
    assert ss.brain_check() == [
        f"🧠 Brain: ❌ memory backend check timed out ({ss.BRAIN_CHECK_TIMEOUT_S}s)"
    ]


def test_brain_check_failure_reports_the_type_not_the_text(monkeypatch, tmp_path):
    (tmp_path / "Makefile").write_text("")
    monkeypatch.setattr(ss, "BRAIN_ENABLED", True)
    monkeypatch.setattr(ss, "MEMORY_REPO_DIR", tmp_path)

    def raises(cmd, timeout):
        raise OSError("postgres://user:secret@host/db")

    monkeypatch.setattr(ss, "_run_in_own_group", raises)
    assert ss.brain_check() == ["🧠 Brain: ⚠️ check failed — OSError"]
