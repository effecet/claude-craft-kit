"""End-to-end tests for tools/mutcheck.py, driven through its CLI against a toy git repo."""

import importlib.util
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

MUTCHECK = Path(__file__).resolve().parents[1] / "tools" / "mutcheck.py"

CALC = """\
def add(a, b):
    return a + b


def clamp(x):
    if x < 0:
        return 0
    return x


def unused():
    return 1
"""

TEST_CALC = """\
from pkg.calc import add, clamp


def test_add():
    assert add(2, 3) == 5


def test_clamp():
    assert clamp(-4) == 0
    assert clamp(7) == 7
"""

TEST_UNUSED = """\
from pkg.calc import unused


def test_unused():
    assert unused() == 1
"""

PLUS = (
    "[[mutant]]\nlabel = 'plus'\nfile = 'pkg/calc.py'\nold = 'a + b'\nnew = 'a - b'\n"
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Private TMPDIR (to see whether the copy was removed) and a clean environment."""
    d = tmp_path / "scratch"
    d.mkdir()
    monkeypatch.setenv("TMPDIR", str(d))
    # No global/system git config: a global excludesfile or core.hooksPath on the
    # developer's machine must not change what the toy repo ignores or runs.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    # PYTHONDONTWRITEBYTECODE: mutcheck sets it itself; inheriting it from an outer
    # run (mutcheck on its own suite) would hide a regression in that setting.
    for var in (
        "PYTEST_ADDOPTS",
        "PYTEST_XDIST_WORKER",
        "PYTEST_XDIST_WORKER_COUNT",
        "PYTHONDONTWRITEBYTECODE",
    ):
        monkeypatch.delenv(var, raising=False)
    return d


@pytest.fixture
def repo(tmp_path: Path, scratch: Path) -> Path:
    r = tmp_path / "repo"
    (r / "pkg").mkdir(parents=True)
    (r / "tests").mkdir()
    (r / "pkg" / "__init__.py").write_text("")
    (r / "pkg" / "calc.py").write_text(CALC)
    (r / "tests" / "test_calc.py").write_text(TEST_CALC)
    (r / ".gitignore").write_text("ignored.txt\n.venv/\n")
    _git(r, "init", "-q")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    return r


def run(
    repo: Path,
    mutants: str,
    *pytest_args: str,
    extra: tuple[str, ...] = (),
    python: str | None = sys.executable,
):
    mfile = repo.parent / "mutants.toml"
    mfile.write_text(mutants)
    args = [sys.executable, str(MUTCHECK), str(mfile), "--repo", str(repo)]
    if python:
        args += ["--python", python]
    args += [*extra, "--", *(pytest_args or ("tests",))]
    return subprocess.run(args, capture_output=True, text=True, timeout=180)


def mutant(label: str, file: str, old: str, new: str, count: int | None = None) -> str:
    def q(s: str) -> str:
        # TOML literal strings: no escapes, so the text lands byte-for-byte
        return "'''" + s + "'''" if "\n" in s else "'" + s + "'"

    body = f"[[mutant]]\nlabel = {label!r}\nfile = {file!r}\nold = {q(old)}\nnew = {q(new)}\n"
    if count is not None:
        body += f"count = {count}\n"
    return body + "\n"


def kept_copy(scratch: Path) -> Path:
    (copy,) = scratch.glob("mutcheck.*")
    return copy


# ---------------------------------------------------------------- outcomes


def test_killed_mutant_reports_assertion_line(repo):
    r = run(
        repo, mutant("plus to minus", "pkg/calc.py", "return a + b", "return a - b")
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "KILLED" in r.stdout
    assert "assert -1 == 5" in r.stdout  # the first E line is echoed


def test_surviving_mutant_exits_one(repo):
    r = run(repo, mutant("unused", "pkg/calc.py", "return 1", "return 2"))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "SURVIVED" in r.stdout


def test_explicit_count_applies_every_occurrence(repo):
    r = run(
        repo, mutant("all", "pkg/calc.py", "    return", "    return -1 or", count=4)
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "KILLED" in r.stdout


def test_multiline_mutant(repo):
    old = "    if x < 0:\n        return 0\n"
    r = run(repo, mutant("drop clamp", "pkg/calc.py", old, ""))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "KILLED" in r.stdout


def test_multiline_mutant_on_crlf_file(repo):
    """A multi-line mutant written (as TOML forces) with LF must still match a
    CRLF file, and must not validate and then replace nothing."""
    (repo / "pkg" / "calc.py").write_bytes(CALC.replace("\n", "\r\n").encode())
    old = "    if x < 0:\n        return 0\n"
    r = run(repo, mutant("drop clamp", "pkg/calc.py", old, ""))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "KILLED" in r.stdout


def test_each_mutant_starts_from_the_original(repo):
    # "unused" normally survives (nothing tests it). Left unrestored, the first
    # mutant would still be in the file and "kill" it.
    m = mutant("minus", "pkg/calc.py", "a + b", "a - b")
    m += mutant("unused", "pkg/calc.py", "return 1", "return 2")
    r = run(repo, m)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "KILLED    minus" in r.stdout
    assert "SURVIVED  unused" in r.stdout


def test_non_python_target_is_not_compiled(repo):
    (repo / "pkg" / "value.txt").write_text("5\n")
    (repo / "tests" / "test_value.py").write_text(
        "from pathlib import Path\n\n"
        "def test_value():\n"
        "    assert int(Path('pkg/value.txt').read_text()) == 5\n"
    )
    # Not valid Python: compiling it would wrongly make this ERRORED.
    r = run(repo, mutant("garble", "pkg/value.txt", "5", "x("), "tests/test_value.py")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "KILLED" in r.stdout


# ---------------------------------------------------------------- ERRORED


def test_syntax_error_mutant_is_errored_not_killed(repo):
    r = run(repo, mutant("broken", "pkg/calc.py", "return a + b", "return a +"))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "ERRORED" in r.stdout
    assert "does not compile" in r.stdout  # caught before any pytest run
    assert "KILLED" not in r.stdout


def test_collection_error_is_errored_not_killed(repo):
    # TEST_CALC imports `add` at module level: pytest exits 2 at collection.
    r = run(repo, mutant("renamed", "pkg/calc.py", "def add(", "def plus("))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "ERRORED" in r.stdout
    assert "KILLED" not in r.stdout


def test_error_raised_inside_a_test_is_errored_not_killed(repo):
    """A lazy import fails at run time: pytest exits 1, like a real kill, and only
    the `E   ImportError: ...` line shows it is not one."""
    (repo / "tests" / "test_calc.py").write_text(
        "def test_add():\n    from pkg.calc import add\n    assert add(2, 3) == 5\n"
    )
    r = run(repo, mutant("renamed", "pkg/calc.py", "def add(", "def plus("))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "ERRORED" in r.stdout
    assert "ImportError" in r.stdout
    assert "KILLED" not in r.stdout


def test_timeout_is_errored(repo):
    m = mutant("hang", "pkg/calc.py", "return a + b", "while True: pass")
    # The baseline has its own budget (--baseline-timeout, default 600), so a
    # short hang budget no longer risks the baseline timing out on a slow runner.
    r = run(repo, m, extra=("--timeout", "3"))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "ERRORED" in r.stdout
    assert "timed out after 3s" in r.stdout


def _alive(pid: int) -> bool:
    """True if `pid` exists and is not a zombie (Linux /proc; macOS reaps)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    try:
        # field 3, after the parenthesised command name, is the state letter
        return stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (FileNotFoundError, IndexError):
        return not stat.parent.parent.exists()  # no /proc (macOS): trust kill


def test_timeout_kills_what_pytest_spawned(repo, tmp_path):
    """A timeout kills pytest's whole process group, not just pytest."""
    pidfile = tmp_path / "child.pid"
    hang = (
        "p = __import__('subprocess').Popen(['sleep', '60'])\n"
        f"    open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
        "    while True: pass"
    )
    # 10s, not 3: the budget also covers pytest starting up in the copy, and
    # the pid file must be written before the kill on a slow runner.
    m = mutant("spawn", "pkg/calc.py", "return a + b", hang)
    r = run(repo, m, extra=("--timeout", "10"))
    assert "timed out after 10s" in r.stdout, r.stdout + r.stderr
    pid = int(pidfile.read_text())
    # A killed child is gone once something reaps it. A container whose PID 1
    # never reaps orphans (some self-hosted CI runners) leaves it a zombie:
    # dead, but still answering os.kill(pid, 0). So a zombie counts as gone.
    for _ in range(50):
        if not _alive(pid):
            return
        time.sleep(0.1)
    os.kill(pid, signal.SIGKILL)  # only here: the pid is known to be ours
    pytest.fail(f"child {pid} outlived the timeout")


@pytest.mark.parametrize("tb", [("--tb=line",), ("--tb=no",), ("--tb", "native")])
def test_tb_styles_without_e_lines_are_refused(repo, tb):
    r = run(repo, PLUS, *tb, "tests")
    assert r.returncode == 2
    assert "prints no E lines" in r.stderr


def test_relative_python_is_resolved_where_mutcheck_runs(repo):
    # A wrapper, not a symlink: a symlink to a venv's python placed outside
    # the venv no longer finds the venv's packages (pytest among them).
    link = repo.parent / "py"
    link.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    link.chmod(0o755)
    mfile = repo.parent / "mutants.toml"
    mfile.write_text(PLUS)
    r = subprocess.run(
        [sys.executable, str(MUTCHECK), str(mfile), "--repo", str(repo),
         "--python", "./py", "--", "tests"],
        cwd=repo.parent,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "KILLED" in r.stdout


def test_addopts_q_still_shows_the_summary(repo):
    (repo / "pytest.ini").write_text("[pytest]\naddopts = -q\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "quiet")
    r = run(repo, PLUS)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "baseline: green" in r.stdout


def test_baseline_timeout_is_reported_as_a_timeout(monkeypatch):
    mc = _load_mutcheck()
    monkeypatch.setattr(mc, "run_pytest", lambda *a: (None, "timed out after 7s"))
    with pytest.raises(mc.SetupError, match=r"baseline timed out after 7s"):
        mc.baseline("python", Path("."), [], 7)


def test_baseline_uses_its_own_budget(repo):
    # A 1s baseline budget aborts setup (exit 2) even with a generous mutant
    # budget, proving the two are not the same knob.
    slow = repo / "tests" / "test_slow.py"
    slow.write_text("import time\n\ndef test_slow():\n    time.sleep(3)\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "slow")
    r = run(repo, PLUS, extra=("--timeout", "600", "--baseline-timeout", "1"))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "baseline timed out after 1s" in r.stderr


def _load_mutcheck():
    spec = importlib.util.spec_from_file_location("mutcheck", MUTCHECK)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mutcheck"] = mod  # @dataclass resolves its module via sys.modules
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    ("rc", "output", "expected"),
    [
        (1, 'E     File "x.py", line 2\nE   SyntaxError: invalid syntax\n', "ERRORED"),
        (1, "E       ModuleNotFoundError: No module named 'pkg'\n", "ERRORED"),
        # the words quoted inside an assertion message are not an exception line
        (
            1,
            "E       AssertionError: got 'E   ERRORED  b  E  SyntaxError: x'\n",
            "KILLED",
        ),
        (1, "E       assert 1 == 2\n", "KILLED"),
        (0, "", "SURVIVED"),
        (2, "E   ImportError: cannot import name 'add'\n", "ERRORED"),
        # exit 2 is a collection error even without an import-error line
        (2, "E   NameError: name 'x' is not defined\n", "ERRORED"),
        (4, "ERROR: usage\n", "ERRORED"),
        (None, "timed out after 3s", "ERRORED"),
        (-1, "E       assert 1 == 2\n", "ERRORED"),  # killed by a signal, not a timeout
    ],
)
def test_classify(rc, output, expected):
    status, _ = _load_mutcheck().classify(rc, output)
    assert status == expected


# ---------------------------------------------------------------- the copy


def test_uncommitted_and_untracked_files_are_synced(repo):
    # The mutant targets text that only exists in the working tree...
    (repo / "pkg" / "calc.py").write_text(
        CALC.replace("return x\n", "return x  # wip\n")
    )
    # ...and an untracked test file is the only one that would kill it.
    (repo / "tests" / "test_extra.py").write_text(TEST_UNUSED)
    m = mutant("wip line", "pkg/calc.py", "return x  # wip", "return -x")
    m += mutant("unused", "pkg/calc.py", "return 1", "return 2")
    r = run(repo, m)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.count("KILLED") == 2


def test_staged_changes_and_deletions_are_synced(repo):
    """The gate runs after `git add -A`: staged-but-uncommitted work must be tested."""
    (repo / "pkg" / "calc.py").write_text(
        CALC.replace("return x\n", "return x  # staged\n")
    )
    (repo / "tests" / "test_staged.py").write_text(TEST_UNUSED)
    (repo / "tests" / "test_doomed.py").write_text(
        "def test_doomed():\n    assert False\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "doomed")
    (repo / "tests" / "test_doomed.py").unlink()
    _git(repo, "add", "-A")  # everything staged, nothing unstaged
    m = mutant("staged line", "pkg/calc.py", "return x  # staged", "return -x")
    m += mutant("unused", "pkg/calc.py", "return 1", "return 2")
    r = run(repo, m)  # a copied test_doomed.py would make the baseline red
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.count("KILLED") == 2


def test_ignored_files_are_not_copied(repo, scratch):
    (repo / "ignored.txt").write_text("secret")
    r = run(repo, PLUS, extra=("--keep",))
    assert r.returncode == 0, r.stdout + r.stderr
    copy = kept_copy(scratch)
    assert not (copy / "ignored.txt").exists()
    assert (copy / "pkg" / "calc.py").exists()


def test_symlinks_and_nested_repos_are_tolerated(repo, tmp_path, scratch):
    """A config checkout can hold untracked symlinks to directories."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / "dirlink").symlink_to(repo / "pkg", target_is_directory=True)
    (repo / "dangling").symlink_to(tmp_path / "nowhere")
    (repo / "tracked_out").symlink_to(outside, target_is_directory=True)
    _git(repo, "add", "tracked_out")
    _git(repo, "commit", "-qm", "symlink out of the repo")
    (repo / "nested").mkdir()
    _git(repo / "nested", "init", "-q")
    (repo / "nested" / "f.txt").write_text("x")
    r = run(repo, PLUS, extra=("--keep",))
    assert r.returncode == 0, r.stdout + r.stderr
    copy = kept_copy(scratch)
    assert (copy / "dirlink").is_symlink()
    assert (copy / "dangling").is_symlink()


def test_copy_is_bytecode_free(repo, scratch):
    """A cached .pyc lets a same-size mutant run the OLD code and falsely survive."""
    (repo / "pkg" / "__pycache__").mkdir()
    (repo / "pkg" / "__pycache__" / "calc.cpython-312.pyc").write_bytes(b"stale")
    # Commit it (-f past any ignore rule) so `git archive` carries it into the copy.
    _git(repo, "add", "-f", "pkg/__pycache__")
    _git(repo, "commit", "-qm", "pyc")
    # --module too: its import probe runs in the copy and must not write bytecode
    r = run(repo, PLUS, extra=("--keep", "--module", "pkg"))
    assert r.returncode == 0, r.stdout + r.stderr
    copy = kept_copy(scratch)
    assert list(copy.rglob("__pycache__")) == []
    assert list(copy.rglob("*.pyc")) == []


def test_original_tree_untouched_and_copy_removed(repo, scratch):
    before = (repo / "pkg" / "calc.py").read_text()
    r = run(repo, PLUS)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (repo / "pkg" / "calc.py").read_text() == before
    assert _git(repo, "status", "--porcelain") == ""
    assert list(scratch.glob("mutcheck.*")) == []


def test_copy_removed_when_setup_fails_part_way(repo, scratch):
    r = run(repo, mutant("typo", "pkg/calc.py", "return a * b", "return a - b"))
    assert r.returncode == 2
    assert list(scratch.glob("mutcheck.*")) == []


# ---------------------------------------------------------------- writes stay in the copy


@pytest.mark.parametrize("kind", ["absolute", "dotdot"])
def test_mutant_path_outside_the_repo_is_rejected(repo, kind):
    target = repo / "pkg" / "calc.py"
    file = str(target) if kind == "absolute" else "../repo/pkg/calc.py"
    r = run(repo, mutant("escape", file, "a + b", "a - b"))
    assert r.returncode == 2
    assert "relative to the repo root" in r.stderr
    assert target.read_text() == CALC


def test_mutant_through_a_symlink_out_of_the_copy_is_rejected(repo, tmp_path):
    victim = tmp_path / "victim.py"
    victim.write_text("x = 1\n")
    (repo / "pkg" / "link.py").symlink_to(victim)
    r = run(repo, mutant("via link", "pkg/link.py", "x = 1", "x = 2"))
    assert r.returncode == 2
    assert "resolves outside the copy" in r.stderr
    assert victim.read_text() == "x = 1\n"


# ---------------------------------------------------------------- setup errors


def test_missing_old_text_aborts_before_running_tests(repo):
    r = run(repo, mutant("typo", "pkg/calc.py", "return a * b", "return a - b"))
    assert r.returncode == 2
    assert "0 occurrence" in r.stderr
    assert "baseline" not in r.stdout.lower()


def test_ambiguous_old_text_needs_explicit_count(repo):
    r = run(repo, mutant("ambiguous", "pkg/calc.py", "return", "return  "))
    assert r.returncode == 2
    assert "expected 1" in r.stderr


def test_zero_collected_tests_aborts(repo):
    (repo / "tests" / "test_empty.py").write_text("x = 1\n")
    r = run(repo, PLUS, "tests/test_empty.py")
    assert r.returncode == 2
    assert "no tests collected" in r.stderr  # mutcheck's message, not pytest's


def test_all_skipped_baseline_aborts(repo):
    """A skipif on a DATABASE_URL the copy has no .env for: green, but nothing ran."""
    (repo / "tests" / "test_skip.py").write_text(
        "import pytest\n\n@pytest.mark.skip(reason='no db')\n"
        "def test_db():\n    assert True\n"
    )
    r = run(repo, PLUS, "tests/test_skip.py")
    assert r.returncode == 2
    assert "no test passed" in r.stderr


def test_failing_baseline_aborts(repo):
    (repo / "pkg" / "calc.py").write_text(CALC.replace("a + b", "a + b + 1"))
    r = run(repo, mutant("unused", "pkg/calc.py", "return 1", "return 2"))
    assert r.returncode == 2
    assert "baseline is not green" in r.stderr


def test_repo_without_commits_is_a_setup_error(tmp_path, scratch):
    empty = tmp_path / "empty"
    empty.mkdir()
    _git(empty, "init", "-q")
    r = run(empty, PLUS)
    assert r.returncode == 2
    assert "git archive HEAD failed" in r.stderr


@pytest.mark.parametrize(
    ("toml", "message"),
    [
        ("[[mutant]]\nlabel = 'x'\n", "missing"),
        (PLUS + "cout = 2\n", "unknown key(s) cout"),
        (PLUS.replace("old = 'a + b'", "old = ''"), "old is empty"),
        (PLUS.replace("new = 'a - b'", "new = 'a + b'"), "identical"),
        (PLUS.replace("old = 'a + b'", "old = 1"), "old must be a string"),
        (PLUS + "count = true\n", "count must be a positive integer"),
    ],
)
def test_bad_mutants_file_is_a_setup_error(repo, toml, message):
    r = run(repo, toml)
    assert r.returncode == 2
    assert message in r.stderr


def test_missing_pytest_args_is_a_usage_error(repo):
    mfile = repo.parent / "mutants.toml"
    mfile.write_text(PLUS)
    r = subprocess.run(
        [sys.executable, str(MUTCHECK), str(mfile), "--repo", str(repo)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 2
    assert "pass the scoped tests after --" in r.stderr


# ---------------------------------------------------------------- --module


def test_module_check_passes_when_import_resolves_into_copy(repo):
    r = run(repo, PLUS, extra=("--module", "pkg"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "pkg ->" in r.stdout


def test_module_check_fails_when_module_missing(repo):
    r = run(repo, PLUS, extra=("--module", "nope"))
    assert r.returncode == 2
    assert "cannot import nope" in r.stderr


def test_module_check_fails_when_module_resolves_outside_the_copy(repo):
    # json imports fine, but from the stdlib: exactly the "tests the original
    # instead of the copy" failure the check exists to catch.
    r = run(repo, PLUS, extra=("--module", "json"))
    assert r.returncode == 2
    assert "not from the copy" in r.stderr


def test_module_must_be_a_module_name(repo):
    r = run(repo, PLUS, extra=("--module", "os; print(1)"))
    assert r.returncode == 2
    assert "is not a module name" in r.stderr


def test_without_module_every_survivor_warns(repo):
    m = mutant("unused", "pkg/calc.py", "return 1", "return 2")
    m += mutant("unused again", "pkg/calc.py", "def unused", "def unused2")
    r = run(repo, m)
    assert r.returncode == 1
    assert "every mutant survived" in r.stderr


# ---------------------------------------------------------------- interpreter


def test_default_python_is_the_repo_venv(repo, tmp_path):
    marker = tmp_path / "venv-used"
    venv_python = repo / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text(
        f'#!/bin/sh\ntouch "{marker}"\nexec "{sys.executable}" "$@"\n'
    )
    venv_python.chmod(0o755)
    r = run(repo, PLUS, python=None)
    assert r.returncode == 0, r.stdout + r.stderr
    assert marker.exists()


def test_xdist_addopts_are_neutralised(repo):
    pytest.importorskip("xdist")
    (repo / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = "-n 2"\n'
    )
    # xdist sets PYTEST_XDIST_WORKER in every worker: this passes only serially.
    (repo / "tests" / "test_serial.py").write_text(
        "import os\n\ndef test_serial():\n"
        "    assert 'PYTEST_XDIST_WORKER' not in os.environ\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "xdist addopts")
    r = run(repo, PLUS)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "KILLED" in r.stdout


def test_user_disabling_xdist_is_respected(repo):
    pytest.importorskip("xdist")
    r = run(repo, PLUS, "-p", "no:xdist", "tests")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "KILLED" in r.stdout


def test_overlay_never_writes_through_a_symlink_from_head(repo, tmp_path):
    """HEAD tracks `d -> outside`; the working tree replaced it with a real dir.
    Overlaying `d/victim.txt` through the copied symlink would overwrite a file
    outside the copy (found in security review)."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.txt").write_text("SECRET")
    (repo / "d").symlink_to(outside, target_is_directory=True)
    _git(repo, "add", "d")
    _git(repo, "commit", "-qm", "d is a symlink")
    (repo / "d").unlink()
    (repo / "d").mkdir()
    (repo / "d" / "victim.txt").write_text("new")
    r = run(repo, PLUS)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (outside / "victim.txt").read_text() == "SECRET"


def test_single_mutant_survivor_warns_without_module(repo):
    r = run(repo, mutant("unused", "pkg/calc.py", "return 1", "return 2"))
    assert r.returncode == 1
    assert "every mutant survived" in r.stderr


@pytest.mark.parametrize(
    ("own", "timeout", "baseline_timeout"),
    [
        ([], 600, 600),
        (["--timeout", "3"], 3, 600),  # a short hang budget keeps the baseline's
        (["--timeout", "1200"], 1200, 1200),  # a large one still covers it
        (["--timeout", "1200", "--baseline-timeout", "5"], 1200, 5),
    ],
)
def test_baseline_timeout_default(own, timeout, baseline_timeout):
    opts, _ = _load_mutcheck().parse_args(["m.toml", *own, "--", "tests"])
    assert (opts.timeout, opts.baseline_timeout) == (timeout, baseline_timeout)


@pytest.mark.parametrize("flag", ["--timeout", "--baseline-timeout"])
def test_timeout_must_be_positive(repo, flag):
    r = run(repo, PLUS, extra=(flag, "0"))
    assert r.returncode == 2
    assert f"argument {flag}: must be at least 1, got 0" in r.stderr


def test_multiline_new_on_crlf_file_keeps_crlf():
    mc = _load_mutcheck()
    m = mc.Mutant("two lines", "f.py", "return a + b", "x = 0\n    return a - b")
    old, new = mc.for_line_endings(m, "def add(a, b):\r\n    return a + b\r\n")
    assert (old, new) == ("return a + b", "x = 0\r\n    return a - b")
    # LF files are left alone
    assert mc.for_line_endings(m, "return a + b\n") == (m.old, m.new)


def test_real_parents_replaces_a_symlinked_ancestor(tmp_path):
    """Defence in depth: git's sorted output already unlinks `d` before `d/x`,
    but a symlinked ancestor must never be written through, whatever the order."""
    mc = _load_mutcheck()
    copy = (tmp_path / "copy").resolve()
    outside = tmp_path / "outside"
    (copy / "a").mkdir(parents=True)
    outside.mkdir()
    (copy / "a" / "d").symlink_to(outside, target_is_directory=True)
    mc.real_parents(copy, copy / "a" / "d" / "e" / "x.txt")
    assert not (copy / "a" / "d").is_symlink()
    assert (copy / "a" / "d" / "e").is_dir()
    assert list(outside.iterdir()) == []


def test_signal_exit_is_not_reported_as_a_timeout():
    status, detail = _load_mutcheck().classify(-1, "E       assert 1 == 2\nmore\n")
    assert status == "ERRORED"
    assert detail == "E       assert 1 == 2"
