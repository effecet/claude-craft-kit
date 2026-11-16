#!/usr/bin/env python3
"""mutcheck — run hand-written mutants against a scoped pytest selection, safely.

Mutation-tests in a private ``git archive`` copy of the repo, never the shared
working tree (rules/development.md § Reviewer agents mutate the SHARED working
tree). Every step that silently tested nothing when this loop was hand-rolled
is a loud failure here:

* a mutant whose ``old`` text is not found exactly ``count`` times aborts the run
  before any test executes (a reformatted line used to make ``sed`` a no-op);
* a baseline that collects 0 tests, is not green, passes none (all
  skipped), or times out aborts the run;
* a mutant ``file`` must resolve inside the copy, so the real tree is never written;
* ``--module`` asserts the package under test imports from the copy;
* a "failure" caused by an import or syntax error, a collection error or a
  timeout is ERRORED, not KILLED;
* surviving mutants are listed, not hidden, so a human can judge equivalence.

Usage:
    mutcheck.py MUTANTS.toml [--repo DIR] [--python PY] [--module NAME]
                [--timeout S] [--baseline-timeout S] [--keep] -- PYTEST_ARGS...

``--timeout`` is the hang budget for each mutant run; ``--baseline-timeout``
is the budget for the green baseline (default: the larger of 600 and
``--timeout``), kept separate so a short hang budget never aborts the run
before a mutant executes, while a large ``--timeout`` still covers the baseline.

MUTANTS.toml:
    [[mutant]]
    label = "drop rollback"
    file  = "pkg/db.py"            # relative to the repo root
    old   = 'con.execute("ROLLBACK")'
    new   = 'con.execute("COMMIT")'
    count = 1                      # optional: occurrences of `old` that must exist

Use TOML literal strings ('...' or '''...''') so backslashes and quotes land
verbatim; ''' strings span lines for multi-line mutants.

Exit codes: 0 all killed · 1 any survived or errored · 2 usage, setup or
validation error (nothing was mutated, or the run cannot be trusted).
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

# An exception line as pytest prints it: `E   SyntaxError: invalid syntax`.
# Anchored so the same words quoted inside an assertion message do not match.
ERROR_LINE = re.compile(
    r"^E\s+(ImportError|ModuleNotFoundError|SyntaxError|IndentationError)\b"
)
PASSED = re.compile(r"\b\d+ passed\b")
# Seconds to drain pytest's output after killing its group on a timeout.
DRAIN_TIMEOUT = 5
# --tb styles that print no `E` lines, so classify() could not tell an
# ImportError or SyntaxError from a real kill.
NO_E_LINES = {"line", "no", "native"}

# Every Python process run inside the copy gets this. A same-size mutant written
# within the same second as a cached .pyc passes Python's mtime+size check, so
# the OLD bytecode runs and the mutant falsely "survives". With no process ever
# writing bytecode in the copy, no code path can reintroduce that.
COPY_ENV = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}

# `git archive` of the repo's own HEAD: members are repo-relative and symlinks
# are reproduced exactly as a checkout would. The "data" filter would reject a
# tracked symlink that points outside the repo, and needs Python 3.11.4+ (some
# distributions still ship 3.11.2). Mutant writes are confined by validate().
TRUSTED = (
    {"filter": "fully_trusted"} if hasattr(tarfile, "fully_trusted_filter") else {}
)

MUTANT_KEYS = {"label", "file", "old", "new", "count"}


class SetupError(Exception):
    """Anything that means the run cannot be trusted; exit code 2."""


class Status(StrEnum):
    KILLED = "KILLED"
    SURVIVED = "SURVIVED"
    ERRORED = "ERRORED"


@dataclass(frozen=True)
class Mutant:
    label: str
    file: str
    old: str
    new: str
    count: int = 1


@dataclass(frozen=True)
class Result:
    mutant: Mutant
    status: Status
    detail: str


# ---------------------------------------------------------------- input


def check_entry(i: int, entry: object) -> Mutant:
    if not isinstance(entry, dict):
        raise SetupError(f"mutant #{i} is not a table")
    unknown = sorted(set(entry) - MUTANT_KEYS)
    if unknown:
        raise SetupError(f"mutant #{i} has unknown key(s) {', '.join(unknown)}")
    missing = [k for k in ("label", "file", "old", "new") if k not in entry]
    if missing:
        raise SetupError(f"mutant #{i} is missing {', '.join(missing)}")
    for key in ("label", "file", "old", "new"):
        if not isinstance(entry[key], str):
            raise SetupError(f"mutant #{i} {key} must be a string")
    count = entry.get("count", 1)
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise SetupError(f"mutant #{i} count must be a positive integer")
    if not entry["old"]:
        raise SetupError(f"mutant #{i} old is empty")
    if entry["old"] == entry["new"]:
        raise SetupError(f"mutant #{i} old and new are identical")
    file = Path(entry["file"])
    if file.is_absolute() or ".." in file.parts:
        raise SetupError(
            f"mutant #{i} file must be relative to the repo root, got {entry['file']}"
        )
    return Mutant(entry["label"], entry["file"], entry["old"], entry["new"], count)


def load_mutants(path: Path) -> list[Mutant]:
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise SetupError(f"cannot read mutants file {path}: {exc}") from exc
    raw = data.get("mutant")
    if not isinstance(raw, list) or not raw:
        raise SetupError(f"{path}: no [[mutant]] entries")
    try:
        return [check_entry(i, entry) for i, entry in enumerate(raw, 1)]
    except SetupError as exc:
        raise SetupError(f"{path}: {exc}") from exc


# ---------------------------------------------------------------- copy


def git(repo: Path, *args: str) -> bytes:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True)
    if proc.returncode != 0:
        raise SetupError(f"git {' '.join(args)} failed: {proc.stderr.decode().strip()}")
    return proc.stdout


def extract_head(repo: Path, dest: Path) -> None:
    """Stream `git archive HEAD` straight into `dest` (no in-memory tarball)."""
    with subprocess.Popen(
        ["git", "archive", "--format=tar", "HEAD"],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as proc:
        try:
            with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
                tar.extractall(dest, **TRUSTED)  # type: ignore[arg-type]
        except tarfile.ReadError:
            pass  # empty stream: git failed, reported below
        _, err = proc.communicate()
    if proc.returncode != 0:
        raise SetupError(f"git archive HEAD failed: {err.decode().strip()}")


def overlay(repo: Path, copy: Path) -> None:
    """Copy into `copy` every working-tree path that differs from HEAD.

    That is everything staged or not (`ls-files -m` alone misses staged work,
    which is exactly the state after a gate's `git add -A`) plus untracked
    non-ignored files. Overlaid files are the WORKING-TREE versions, so unstaged
    edits made after `git add -A` are tested too. Symlinks are copied as links;
    directories (submodules, nested repos) are left as HEAD has them.
    """
    tracked = git(repo, "diff", "-z", "--name-only", "--no-renames", "HEAD")
    untracked = git(repo, "ls-files", "-z", "-o", "--exclude-standard")
    for raw in filter(None, (tracked + b"\0" + untracked).split(b"\0")):
        rel = os.fsdecode(raw)
        src, dst = repo / rel, copy / rel
        if src.is_dir() and not src.is_symlink():
            if dst.is_symlink():  # a symlink in HEAD, a real dir now
                dst.unlink()
            continue
        if dst.is_dir() and not dst.is_symlink():
            continue
        real_parents(copy, dst)
        if os.path.lexists(dst):
            dst.unlink()
        if not os.path.lexists(src):  # deleted in the working tree or the index
            continue
        shutil.copy2(src, dst, follow_symlinks=False)


def real_parents(copy: Path, dst: Path) -> None:
    """Make every ancestor of `dst` inside the copy a real directory.

    git lists no path beneath a symlink, so a listed path's ancestors are real
    directories in the working tree. One that is a symlink in the copy came from
    HEAD (a dir that used to be a link); writing through it would land outside
    the copy, so it is replaced, as the working tree has it.
    """
    cur = copy
    for part in dst.relative_to(copy).parts[:-1]:
        cur = cur / part
        if cur.is_symlink():
            cur.unlink()
        cur.mkdir(exist_ok=True)
    if not dst.parent.resolve().is_relative_to(copy):
        raise SetupError(
            f"refusing to write {dst}: its directory resolves outside the copy"
        )


def make_copy(repo: Path, copy: Path) -> None:
    extract_head(repo, copy)
    overlay(repo, copy)
    # Committed or overlaid bytecode would be stale for the same reason COPY_ENV
    # exists; nothing run in the copy writes any more of it.
    for cache in list(copy.rglob("__pycache__")):
        shutil.rmtree(cache, ignore_errors=True)


# ---------------------------------------------------------------- checks


def read_source(path: Path) -> str:
    """One decoding for counting AND replacing. `read_text()` would turn CRLF
    into LF, so a multi-line `old` could count 1 and then replace nothing."""
    return path.read_bytes().decode()


def for_line_endings(m: Mutant, text: str) -> tuple[str, str]:
    """`old`/`new` as they appear in a CRLF file. TOML normalises the newlines in
    a ''' string to LF, so a mutants file cannot spell CRLF itself."""
    if "\r\n" not in text:
        return m.old, m.new

    def crlf(s: str) -> str:
        return s if "\r\n" in s else s.replace("\n", "\r\n")

    return crlf(m.old), crlf(m.new)


def target_path(copy: Path, m: Mutant) -> Path:
    return (copy / m.file).resolve()


def validate(copy: Path, mutants: list[Mutant]) -> None:
    problems = []
    for m in mutants:
        target = target_path(copy, m)
        if not target.is_relative_to(copy):
            problems.append(
                f"[{m.label}] {m.file}: resolves outside the copy ({target})"
            )
            continue
        if not target.is_file():
            problems.append(f"[{m.label}] {m.file}: no such file in the copy")
            continue
        try:
            text = read_source(target)
        except UnicodeDecodeError:
            problems.append(f"[{m.label}] {m.file}: not UTF-8")
            continue
        found = text.count(for_line_endings(m, text)[0])
        if found != m.count:
            problems.append(
                f"[{m.label}] {m.file}: {found} occurrence(s) of `old`, expected {m.count}"
            )
    if problems:
        raise SetupError("invalid mutants:\n  " + "\n  ".join(problems))


def run_in_copy(python: str, copy: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [python, *args],
        cwd=copy,
        env=COPY_ENV,
        capture_output=True,
        text=True,
        errors="replace",
    )


def check_module(python: str, copy: Path, module: str) -> str:
    if not all(part.isidentifier() for part in module.split(".")):
        raise SetupError(f"--module {module!r} is not a module name")
    proc = run_in_copy(python, copy, "-c", f"import {module} as m; print(m.__file__)")
    if proc.returncode != 0:
        raise SetupError(
            f"cannot import {module} from the copy:\n{proc.stderr.strip()}"
        )
    where = Path(proc.stdout.strip()).resolve()
    if not where.is_relative_to(copy):
        raise SetupError(f"{module} imports from {where}, not from the copy {copy}")
    return str(where)


def has_xdist(python: str, copy: Path) -> bool:
    return run_in_copy(python, copy, "-c", "import xdist").returncode == 0


# ---------------------------------------------------------------- run


def run_pytest(
    python: str, copy: Path, args: list[str], timeout: int
) -> tuple[int | None, str]:
    """Run pytest in its own process group, so a timeout kills what it spawned.

    The exit code is None on a timeout (-1 would be a real SIGHUP exit)."""
    # No -q of our own: a repo's `addopts = -q` plus ours is -qq, and -qq drops
    # the summary line baseline() reads.
    cmd = [python, "-m", "pytest", "-p", "no:cacheprovider", *args]
    with subprocess.Popen(
        cmd,
        cwd=copy,
        env=COPY_ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        start_new_session=True,
    ) as proc:
        try:
            output, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_group(proc)
            try:
                proc.communicate(timeout=DRAIN_TIMEOUT)
            except subprocess.TimeoutExpired:
                pass  # something outside the group still holds the pipe
            return None, f"timed out after {timeout}s"
        except BaseException:  # Ctrl-C: the group is in its own session
            kill_group(proc)
            raise
    return proc.returncode, output


def kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL pytest's whole process group, so children it spawned die too."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # Gone already; macOS reports a group whose only member is an unreaped
        # leader as EPERM rather than ESRCH.
        proc.kill()


def first_line(output: str, match: Callable[[str], object]) -> str:
    return next((ln.strip() for ln in output.splitlines() if match(ln)), "")


def classify(rc: int | None, output: str) -> tuple[Status, str]:
    if rc is None:  # timed out
        return Status.ERRORED, output
    if rc == 0:
        return Status.SURVIVED, ""
    eline = first_line(output, lambda ln: ln.startswith("E "))
    if rc != 1:  # 2 interrupted/collection error, 3 internal, 4 usage, 5 no tests
        return Status.ERRORED, eline or f"pytest exit {rc}"
    # A SyntaxError/ImportError raised inside a test is reported as
    # `E     File "...", line N` first and `E   SyntaxError: ...` a few lines
    # later, so every E line is checked, not only the first.
    marker = first_line(output, ERROR_LINE.match)
    if marker:
        return Status.ERRORED, marker
    return Status.KILLED, eline or "(no E line)"


def run_mutant(
    python: str, copy: Path, m: Mutant, args: list[str], timeout: int
) -> Result:
    target = target_path(copy, m)
    original = target.read_bytes()
    text = read_source(target)
    mutated = text.replace(*for_line_endings(m, text))
    if target.suffix == ".py":
        try:
            compile(mutated, m.file, "exec")
        except SyntaxError as exc:  # IndentationError included
            detail = f"does not compile: {exc.msg} (line {exc.lineno})"
            return Result(m, Status.ERRORED, detail)
    try:
        target.write_bytes(mutated.encode())
        rc, output = run_pytest(python, copy, args, timeout)
    finally:
        target.write_bytes(original)
    status, detail = classify(rc, output)
    return Result(m, status, detail)


def default_python(repo: Path) -> str:
    venv = repo / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def pytest_flags(python: str, copy: Path, pytest_args: list[str]) -> list[str]:
    # --tb=short guarantees the E lines classify() reads; a user --tb later in
    # pytest_args still wins. -n0 neutralises a repo's `addopts = -n auto`,
    # unless the user already disabled xdist (then -n is an unknown option).
    flags = ["--tb=short"]
    if has_xdist(python, copy) and not any("no:xdist" in a for a in pytest_args):
        flags.append("-n0")
    return flags + pytest_args


def baseline(python: str, copy: Path, args: list[str], timeout: int) -> str:
    rc, output = run_pytest(python, copy, args, timeout)
    if rc is None:
        raise SetupError(
            f"baseline timed out after {timeout}s (raise --baseline-timeout)"
        )
    if rc == 5:
        raise SetupError(f"baseline: no tests collected by {' '.join(args)}")
    if rc != 0:
        tail = "\n".join(output.splitlines()[-15:])
        raise SetupError(f"baseline is not green (pytest exit {rc}):\n{tail}")
    summary = next(
        (ln.strip() for ln in reversed(output.splitlines()) if PASSED.search(ln)), ""
    )
    if not summary:
        # Every test skipped or deselected (e.g. a skipif on a DATABASE_URL the
        # copy has no .env for): every mutant would "survive".
        last = output.strip().splitlines()[-1] if output.strip() else "no output"
        raise SetupError(
            f"baseline: no test passed ({last}). If pytest ran as -qq (a -q in "
            "addopts plus one in the args), the summary line is hidden: drop one."
        )
    return summary


# ---------------------------------------------------------------- cli


def positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {n}")
    return n


def parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    if "--" in argv:
        split = argv.index("--")
        own, pytest_args = argv[:split], argv[split + 1 :]
    else:
        own, pytest_args = argv, []
    p = argparse.ArgumentParser(prog="mutcheck", description=__doc__.split("\n")[0])
    p.add_argument("mutants", type=Path)
    p.add_argument("--repo", type=Path, default=Path.cwd())
    p.add_argument("--python", help="interpreter for pytest (default: <repo>/.venv)")
    p.add_argument("--module", help="package that must import from the copy")
    p.add_argument(
        "--timeout",
        type=positive_int,
        default=600,
        help="seconds per mutant run before it counts as a hang (ERRORED)",
    )
    p.add_argument(
        "--baseline-timeout",
        type=positive_int,
        help="seconds for the green baseline run (default: max(600, --timeout))",
    )
    p.add_argument("--keep", action="store_true", help="keep the copy for inspection")
    opts = p.parse_args(own)
    if opts.baseline_timeout is None:
        opts.baseline_timeout = max(600, opts.timeout)
    if not pytest_args:
        p.error("pass the scoped tests after --, e.g. -- tests/test_x.py")
    for i, a in enumerate(pytest_args):
        style = a[5:] if a.startswith("--tb=") else None
        if a == "--tb" and i + 1 < len(pytest_args):
            style = pytest_args[i + 1]
        if style in NO_E_LINES:
            p.error(f"--tb={style} prints no E lines, so errors would read as kills")
    # A relative --python names a file relative to where you ran mutcheck, but
    # pytest runs inside the copy: pin it now. A bare name stays a PATH lookup.
    if opts.python and os.sep in opts.python:
        opts.python = str(Path(opts.python).absolute())
    return opts, pytest_args


def report(results: list[Result], module: str | None) -> int:
    width = max(len(r.mutant.label) for r in results)
    for r in results:
        print(f"{r.status:<9} {r.mutant.label:<{width}}  {r.detail}".rstrip())
    tally = {s: sum(r.status is s for r in results) for s in Status}
    print(", ".join(f"{n} {s.lower()}" for s, n in tally.items()))
    if not module and tally[Status.SURVIVED] == len(results):
        print(
            "warning: every mutant survived; if imports resolve to the original "
            "tree instead of the copy, nothing was tested. Re-run with --module.",
            file=sys.stderr,
        )
    return 0 if tally[Status.KILLED] == len(results) else 1


def main(argv: list[str] | None = None) -> int:
    opts, pytest_args = parse_args(sys.argv[1:] if argv is None else argv)
    copy: Path | None = None
    try:
        repo = Path(git(opts.repo, "rev-parse", "--show-toplevel").decode().strip())
        python = opts.python or default_python(repo)
        mutants = load_mutants(opts.mutants)
        # Created here, not in make_copy, so a failure part-way still cleans up.
        copy = Path(tempfile.mkdtemp(prefix="mutcheck.")).resolve()  # honours TMPDIR
        print(f"copy: {copy}")
        make_copy(repo, copy)
        validate(copy, mutants)
        if opts.module:
            print(f"import: {opts.module} -> {check_module(python, copy, opts.module)}")
        else:
            print(
                "note: no --module; the import location is not checked", file=sys.stderr
            )
        args = pytest_flags(python, copy, pytest_args)
        summary = baseline(python, copy, args, opts.baseline_timeout)
        print(f"baseline: green ({summary})")
        results = [run_mutant(python, copy, m, args, opts.timeout) for m in mutants]
    except (SetupError, OSError, UnicodeError, tarfile.TarError) as exc:
        print(f"mutcheck: {exc}", file=sys.stderr)
        return 2
    finally:
        if copy is not None and not opts.keep:
            shutil.rmtree(copy, ignore_errors=True)
        elif copy is not None:
            print(f"kept: {copy}")
    return report(results, opts.module)


if __name__ == "__main__":
    sys.exit(main())
