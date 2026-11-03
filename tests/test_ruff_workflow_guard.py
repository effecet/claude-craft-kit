"""Tests for ruff_workflow_guard.py.

Covers the activation matrix (all 5 preconditions must hit before ruff runs)
plus the block/allow exit codes via the main() entry point.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

import ruff_workflow_guard as rwg  # noqa: E402


class TestIsGitCommit:
    @pytest.mark.parametrize(
        "command",
        [
            "git commit -m 'msg'",
            "git commit -am msg",
            "git -C /repo commit -m msg",
            "  git commit",
            "cd /tmp && git commit -m x",
            "git status; git commit",
            "(git commit)",
            "bash -c 'git commit'",
            'eval "git commit"',
        ],
    )
    def test_matches(self, command: str) -> None:
        assert rwg._is_git_commit(command) is True

    @pytest.mark.parametrize(
        "command",
        [
            "git status",
            "git push",
            "gitcommit",
            "ungit commit",
            "git committed",
            "git commit-tree HEAD",
            "git commit-graph write",
            "",
        ],
    )
    def test_does_not_match(self, command: str) -> None:
        assert rwg._is_git_commit(command) is False


class TestHasWorkflowDir:
    def test_forgejo_workflows(self, tmp_path: Path) -> None:
        (tmp_path / ".forgejo" / "workflows").mkdir(parents=True)
        assert rwg._has_workflow_dir(tmp_path) is True

    def test_github_workflows(self, tmp_path: Path) -> None:
        (tmp_path / ".github" / "workflows").mkdir(parents=True)
        assert rwg._has_workflow_dir(tmp_path) is True

    def test_neither(self, tmp_path: Path) -> None:
        assert rwg._has_workflow_dir(tmp_path) is False

    def test_workflow_file_not_dir(self, tmp_path: Path) -> None:
        (tmp_path / ".github").mkdir()
        (tmp_path / ".github" / "workflows").write_text("not a dir")
        assert rwg._has_workflow_dir(tmp_path) is False


class TestHasRuffConfig:
    def test_no_pyproject(self, tmp_path: Path) -> None:
        assert rwg._has_ruff_config(tmp_path) is False

    def test_pyproject_without_ruff(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname='x'\n[tool.black]\nline-length=100\n"
        )
        assert rwg._has_ruff_config(tmp_path) is False

    def test_pyproject_with_ruff_section(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname='x'\n\n[tool.ruff]\nline-length = 100\n"
        )
        assert rwg._has_ruff_config(tmp_path) is True

    def test_pyproject_with_ruff_subsection(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[tool.ruff.lint]\nselect = ['E']\n"
        )
        assert rwg._has_ruff_config(tmp_path) is True

    def test_string_literal_mentioning_ruff_does_not_match(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname="x"\ndescription="docs reference [tool.ruff] config"\n'
        )
        assert rwg._has_ruff_config(tmp_path) is False

    def test_malformed_toml_returns_false(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("this is not [valid toml")
        assert rwg._has_ruff_config(tmp_path) is False


def _make_input(tool_name: str = "Bash", command: str = "") -> str:
    return json.dumps({"tool_name": tool_name, "tool_input": {"command": command}})


class TestMain:
    @pytest.fixture
    def workflow_repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        (tmp_path / ".github" / "workflows").mkdir(parents=True)
        (tmp_path / "pyproject.toml").write_text(
            "[tool.ruff]\nline-length = 100\n"
        )
        monkeypatch.chdir(tmp_path)
        return tmp_path

    def _run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        payload: str,
        ruff_result: tuple[int, str] = (0, ""),
        ruff_on_path: bool = True,
    ) -> tuple[int, str]:
        monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
        monkeypatch.setattr(
            rwg.shutil, "which", lambda _: "/usr/bin/ruff" if ruff_on_path else None
        )
        monkeypatch.setattr(rwg, "_run_ruff", lambda *_a: ruff_result)
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        rc = rwg.main()
        return rc, sys.stderr.getvalue()  # type: ignore[attr-defined]

    def test_non_bash_tool_allows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rc, _ = self._run(
            monkeypatch, _make_input(tool_name="Edit", command="git commit")
        )
        assert rc == 0

    def test_bash_non_commit_allows(
        self, monkeypatch: pytest.MonkeyPatch, workflow_repo: Path
    ) -> None:
        rc, _ = self._run(monkeypatch, _make_input(command="git push"))
        assert rc == 0

    def test_commit_no_workflow_dir_allows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nx=1\n")
        monkeypatch.chdir(tmp_path)
        rc, _ = self._run(monkeypatch, _make_input(command="git commit -m x"))
        assert rc == 0

    def test_commit_no_ruff_config_allows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        (tmp_path / ".github" / "workflows").mkdir(parents=True)
        monkeypatch.chdir(tmp_path)
        rc, _ = self._run(monkeypatch, _make_input(command="git commit -m x"))
        assert rc == 0

    def test_commit_no_ruff_binary_allows(
        self, monkeypatch: pytest.MonkeyPatch, workflow_repo: Path
    ) -> None:
        rc, _ = self._run(
            monkeypatch,
            _make_input(command="git commit -m x"),
            ruff_on_path=False,
        )
        assert rc == 0

    def test_commit_clean_allows(
        self, monkeypatch: pytest.MonkeyPatch, workflow_repo: Path
    ) -> None:
        rc, _ = self._run(
            monkeypatch,
            _make_input(command="git commit -m x"),
            ruff_result=(0, ""),
        )
        assert rc == 0

    def test_commit_dirty_blocks(
        self, monkeypatch: pytest.MonkeyPatch, workflow_repo: Path
    ) -> None:
        rc, stderr = self._run(
            monkeypatch,
            _make_input(command="git commit -m x"),
            ruff_result=(1, "E501 line too long"),
        )
        assert rc == 2
        assert "RUFF PRE-COMMIT GATE FAILED" in stderr
        assert "E501" in stderr

    def test_malformed_input_allows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO("not-json"))
        assert rwg.main() == 0

    def test_null_json_allows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO("null"))
        assert rwg.main() == 0

    def test_array_json_allows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO("[1,2,3]"))
        assert rwg.main() == 0

    @pytest.mark.parametrize(
        "payload",
        [
            '{"tool_name":"Bash"}',
            '{"tool_name":"Bash","tool_input":null}',
            '{"tool_name":"Bash","tool_input":{"command":123}}',
            '{"tool_name":"Bash","tool_input":{}}',
        ],
    )
    def test_tool_input_shape_variants_allow(
        self, monkeypatch: pytest.MonkeyPatch, payload: str
    ) -> None:
        rc, _ = self._run(monkeypatch, payload)
        assert rc == 0


class TestRunRuff:
    @pytest.fixture(autouse=True)
    def one_file_in_scope(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # These tests fake subprocess.run for ruff; keep file listing out of it.
        monkeypatch.setattr(rwg, "_files_in_commit_scope", lambda *_a: ["a.py"])
        monkeypatch.setattr(rwg, "_ruff_discovered_files", lambda _cwd: {"a.py"})

    def test_check_failure_returns_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []

        def fake_run(args: list[str], **kwargs: Any) -> mock.Mock:
            calls.append(args)
            result = mock.Mock()
            result.returncode = 1 if args[1] == "check" else 0
            result.stdout = "E501 line too long" if args[1] == "check" else ""
            result.stderr = ""
            return result

        monkeypatch.setattr(rwg.subprocess, "run", fake_run)
        code, output = rwg._run_ruff(tmp_path)
        assert code == 1
        assert "E501" in output
        # Must short-circuit — only `ruff check` ran, not `ruff format`.
        assert len(calls) == 1

    def test_format_failure_returns_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(args: list[str], **kwargs: Any) -> mock.Mock:
            result = mock.Mock()
            # check passes, format fails
            result.returncode = 0 if args[1] == "check" else 1
            result.stdout = "Would reformat: file.py" if args[1] == "format" else ""
            result.stderr = ""
            return result

        monkeypatch.setattr(rwg.subprocess, "run", fake_run)
        code, output = rwg._run_ruff(tmp_path)
        assert code == 1
        assert "Would reformat" in output

    def test_both_pass_returns_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(args: list[str], **kwargs: Any) -> mock.Mock:
            result = mock.Mock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        monkeypatch.setattr(rwg.subprocess, "run", fake_run)
        code, output = rwg._run_ruff(tmp_path)
        assert code == 0
        assert output == ""

    def test_ruff_missing_logs_and_allows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(*_args: Any, **_kwargs: Any) -> mock.Mock:
            raise FileNotFoundError("no ruff")

        monkeypatch.setattr(rwg.subprocess, "run", fake_run)
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        code, output = rwg._run_ruff(tmp_path)
        assert code == 0
        assert output == ""
        # Loud-fail (a silent guard is a useless guard): stderr must mention failure.
        assert "ruff invocation failed" in sys.stderr.getvalue()  # type: ignore[attr-defined]

    def test_argv_too_long_logs_and_allows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A huge repo's file list can exceed ARG_MAX: E2BIG is an OSError.
        def fake_run(*_args: Any, **_kwargs: Any) -> mock.Mock:
            raise OSError(7, "Argument list too long")

        monkeypatch.setattr(rwg.subprocess, "run", fake_run)
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        assert rwg._run_ruff(tmp_path) == (0, "")
        assert "ruff invocation failed" in sys.stderr.getvalue()  # type: ignore[attr-defined]

    def test_ruff_timeout_logs_and_allows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess as sp

        def fake_run(*_args: Any, **_kwargs: Any) -> mock.Mock:
            raise sp.TimeoutExpired(cmd="ruff", timeout=30)

        monkeypatch.setattr(rwg.subprocess, "run", fake_run)
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        code, output = rwg._run_ruff(tmp_path)
        assert code == 0
        assert output == ""
        assert "ruff invocation failed" in sys.stderr.getvalue()  # type: ignore[attr-defined]

    def test_stderr_only_failure_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(args: list[str], **_kwargs: Any) -> mock.Mock:
            result = mock.Mock()
            result.returncode = 1 if args[1] == "check" else 0
            result.stdout = ""
            result.stderr = "fatal: bad config"
            return result

        monkeypatch.setattr(rwg.subprocess, "run", fake_run)
        code, output = rwg._run_ruff(tmp_path)
        assert code == 1
        assert "fatal: bad config" in output


BAD_PY = "import os\n"  # F401: unused import
REAL_RUFF = shutil.which("ruff")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A real git repo: CI lints a clean checkout, i.e. tracked files only."""
    _git(tmp_path, "init", "-q")
    return tmp_path


@pytest.fixture
def ruff_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record every real `ruff check` argv, so a pass proves ruff ran."""
    calls: list[list[str]] = []
    real_run = subprocess.run

    def spy(args: list[str], **kwargs: Any) -> Any:
        if args[:2] == ["ruff", "check"] and "--show-files" not in args:
            calls.append(args)
        return real_run(args, **kwargs)

    monkeypatch.setattr(rwg.subprocess, "run", spy)
    return calls


def _linted(calls: list[list[str]]) -> list[str]:
    """The paths the single real `ruff check` run was given."""
    assert len(calls) == 1, calls
    return calls[0][calls[0].index("--") + 1 :]


@pytest.fixture
def recorded_runs(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Fake a passing subprocess.run and record each argv it was given."""
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: Any) -> mock.Mock:
        calls.append(args)
        return mock.Mock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(rwg.subprocess, "run", fake_run)
    return calls


class TestGitAddPathspecs:
    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("git commit -m x", None),
            ('git commit -m "add the thing"', None),
            ("git add -u && git commit", None),
            ("git add a.py && git commit", ["a.py"]),
            ("git add a.py; git add b.py", ["a.py", "b.py"]),
            ("git add . && git commit", ["."]),
            ("git add -A src", ["src"]),
            ("git add -- -weird.py", ["-weird.py"]),
            ("git -c user.name=x add z && git commit", ["z"]),
            ("git stage new.py && git commit", ["new.py"]),
            ("git add a.py 2>&1 && git commit", ["a.py"]),
            ("git add a.py > add.log && git commit", ["a.py"]),
            ("git add -A && git commit", []),
            ("git add -Av && git commit", []),
            ("git add -fA && git commit", []),
            # shell_words reads these, so they are no longer strict.
            ("bash -c 'git add x && git commit'", ["x"]),
            ("cd sub && git add x.py && git commit", ["x.py"]),
            ("pushd sub && git add x.py && git commit", ["x.py"]),
            ("builtin cd sub && git add x.py && git commit", ["x.py"]),
            ("cd sub && git add x.py && cd .. && git commit", ["sub/x.py"]),
            ("git -C sub add x.py && git commit", ["sub/x.py"]),
            ("(cd sub && git add x.py) && git commit", ["sub/x.py"]),
            ("git add a.py &>add.log && git commit", ["a.py"]),
            # Unreadable forms resolve to "every untracked file" — strict.
            ("eval 'git add x' && git commit", []),
            ("git -C /other add y && git commit", []),
            ('git add "unbalanced', []),
            ("git add --pathspec-from-file=list && git commit", []),
            ('git add "$F" && git commit', []),
            ("git add $(git ls-files -o) && git commit", []),
            ("git add `ls` && git commit", []),
            ("git add new{1,2}.py && git commit", []),
            ("git add ~/x.py && git commit", []),
            ("popd && git add x.py && git commit", []),
            ('cd "$D" && git add x.py && git commit', []),
            ("bash -c 'cd sub' && git add x.py && git commit", []),
            ("sh -c 'cd sub && git add x.py' && git commit", []),
            ("cd -P sub && git add x.py && git commit", []),
            ("cd -- sub && git add x.py && git commit", []),
            ("cd - && git add x.py && git commit", []),
            ("git --git-dir=/x add y && git commit", []),
            ("CDPATH=. cd sub && git add x.py && git commit", []),
        ],
    )
    def test_parses_what_the_command_will_stage(
        self, command: str, expected: list[str] | None
    ) -> None:
        assert rwg._git_add_pathspecs(command, Path("/repo")) == expected


class TestCommitPlan:
    @pytest.mark.parametrize(
        ("command", "commit_dir"),
        [
            ("git commit -m x", "/repo"),
            ("cd sub && git commit -m x", "/repo/sub"),
            ("git -C sub commit -m x", "/repo/sub"),
            ("(cd sub && git commit -m x)", "/repo/sub"),
            ("(cd sub) && git commit -m x", "/repo"),
            ("cd /elsewhere && git commit -m x", "/elsewhere"),
            ('cd "$D" && git commit -m x', "/repo"),  # unknowable: start
            ("bash -c 'cd sub' && git commit -m x", "/repo"),  # strict: start
            ("cd -P sub && git commit -m x", "/repo"),
        ],
    )
    def test_commit_runs_where_the_command_says(
        self, command: str, commit_dir: str
    ) -> None:
        assert rwg._commit_plan(command, Path("/repo"))[0] == Path(commit_dir)


class TestTildeInCdTargets:
    """A bare `~` in a cd / -C target is the home dir, not "unknowable" (treating
    it as unknowable made `cd ~/somerepo && git commit` lint untracked files the
    commit could never include). Quoted `~`, `~user` and other expansions stay strict."""

    @pytest.fixture(autouse=True)
    def _home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", "/hm/u")

    @pytest.mark.parametrize(
        ("command", "commit_dir", "specs"),
        [
            ("cd ~/.claude && git commit -m x", "/hm/u/.claude", None),
            ("cd ~ && git commit -m x", "/hm/u", None),
            ("pushd ~/repo && git add a.py && git commit", "/hm/u/repo", ["a.py"]),
            ("git -C ~/repo commit -m x", "/hm/u/repo", None),
            ("git -C ~/repo add a.py && git -C ~/repo commit", "/hm/u/repo", ["a.py"]),
            ("cd ~/repo/sub && git add a.py && cd .. && git commit", "/hm/u/repo", ["sub/a.py"]),
        ],
    )
    def test_bare_tilde_resolves_to_home(
        self, command: str, commit_dir: str, specs: list[str] | None
    ) -> None:
        assert rwg._commit_plan(command, Path("/repo")) == (Path(commit_dir), specs)

    @pytest.mark.parametrize(
        "command",
        [
            'cd "~/repo" && git commit -m x',  # quoted: the shell keeps a literal ~
            "cd '~/repo' && git commit -m x",
            "cd ~other/repo && git commit -m x",  # another user's home
            "cd ~/$D && git commit -m x",
            "cd ~/{a,b} && git commit -m x",
            'git -C "~/repo" commit -m x',
            "git -C ~other commit -m x",
            "cd ~/repo && git add '~/x.py' && git commit",  # any quoted ~ → strict
            "cd \\~/repo && git commit -m x",  # escaped: the shell runs in ./~/repo
            "export HOME=/tmp; cd ~/repo && git commit -m x",
            "HOME=/tmp cd ~/repo && git commit -m x",
        ],
    )
    def test_quoted_or_other_tilde_forms_stay_strict(self, command: str) -> None:
        assert rwg._commit_plan(command, Path("/repo")) == (Path("/repo"), [])

    def test_tilde_in_a_git_add_path_stays_strict(self) -> None:
        assert rwg._git_add_pathspecs("cd ~/repo && git add ~/repo/a.py && git commit", Path("/r")) == []


class TestFilesInCommitScope:
    def test_tracked_files_only_for_a_plain_commit(self, git_repo: Path) -> None:
        (git_repo / "tracked.py").write_text("x = 1\n")
        (git_repo / "untracked.py").write_text(BAD_PY)
        _git(git_repo, "add", "tracked.py")
        assert rwg._files_in_commit_scope(git_repo, "git commit -m x") == ["tracked.py"]

    def test_includes_files_the_same_command_adds(self, git_repo: Path) -> None:
        (git_repo / "new.py").write_text(BAD_PY)
        (git_repo / "other.py").write_text(BAD_PY)
        scope = rwg._files_in_commit_scope(git_repo, "git add new.py && git commit")
        assert scope == ["new.py"]

    def test_add_all_includes_every_untracked_file(self, git_repo: Path) -> None:
        (git_repo / "a.py").write_text("x = 1\n")
        (git_repo / "b.py").write_text("x = 1\n")
        scope = rwg._files_in_commit_scope(git_repo, "git add -A && git commit")
        assert scope == ["a.py", "b.py"]

    def test_skips_tracked_files_deleted_from_disk(self, git_repo: Path) -> None:
        (git_repo / "kept.py").write_text("x = 1\n")
        (git_repo / "gone.py").write_text("x = 1\n")
        _git(git_repo, "add", "kept.py", "gone.py")
        (git_repo / "gone.py").unlink()
        assert rwg._files_in_commit_scope(git_repo, "git commit") == ["kept.py"]

    def test_outside_a_git_repo_returns_none(self, tmp_path: Path) -> None:
        assert rwg._files_in_commit_scope(tmp_path, "git commit") is None

    def test_non_utf8_name_does_not_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # APFS refuses such names, so fake git's output rather than the file.
        monkeypatch.setattr(
            rwg.subprocess,
            "run",
            lambda *_a, **_k: mock.Mock(returncode=0, stdout=b"ok.py\0bad\xff.py\0"),
        )
        (tmp_path / "ok.py").write_text("x = 1\n")
        assert rwg._git_files(tmp_path, ["ls-files", "-z"]) == ["ok.py"]


class TestRunRuffScope:
    def test_lints_scope_files_ruff_would_see_after_end_of_options(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        recorded_runs: list[list[str]],
    ) -> None:
        monkeypatch.setattr(
            rwg,
            "_files_in_commit_scope",
            lambda *_a: ["README.md", "a.py", "b/c.py"],
        )
        monkeypatch.setattr(
            rwg, "_ruff_discovered_files", lambda _c: {"a.py", "b/c.py"}
        )
        assert rwg._run_ruff(tmp_path) == (0, "")
        assert recorded_runs == [
            ["ruff", "check", "--force-exclude", "--", "a.py", "b/c.py"],
            ["ruff", "format", "--check", "--force-exclude", "--", "a.py", "b/c.py"],
        ]

    def test_empty_scope_skips_ruff_loudly(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        recorded_runs: list[list[str]],
    ) -> None:
        monkeypatch.setattr(rwg, "_files_in_commit_scope", lambda *_a: [])
        monkeypatch.setattr(rwg, "_ruff_discovered_files", lambda _c: {"a.py"})
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        assert rwg._run_ruff(tmp_path) == (0, "")
        assert recorded_runs == []
        assert "no files in commit scope" in sys.stderr.getvalue()  # type: ignore[attr-defined]

    @pytest.mark.parametrize(
        ("scope", "discovered"), [(None, {"a.py"}), (["a.py"], None)]
    )
    def test_unlistable_files_fall_back_to_whole_tree_loudly(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        recorded_runs: list[list[str]],
        scope: list[str] | None,
        discovered: set[str] | None,
    ) -> None:
        monkeypatch.setattr(rwg, "_files_in_commit_scope", lambda *_a: scope)
        monkeypatch.setattr(rwg, "_ruff_discovered_files", lambda _c: discovered)
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        rwg._run_ruff(tmp_path)
        assert recorded_runs[-2] == ["ruff", "check", "."]
        assert "linting the whole tree" in sys.stderr.getvalue()  # type: ignore[attr-defined]

    def test_failure_output_names_the_file_count_not_every_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        files = [f"f{n}.py" for n in range(300)]
        monkeypatch.setattr(rwg, "_files_in_commit_scope", lambda *_a: files)
        monkeypatch.setattr(rwg, "_ruff_discovered_files", lambda _c: set(files))
        monkeypatch.setattr(
            rwg.subprocess,
            "run",
            lambda *_a, **_k: mock.Mock(returncode=1, stdout="F401", stderr=""),
        )
        _code, output = rwg._run_ruff(tmp_path)
        assert output.splitlines()[0] == (
            "$ ruff check --force-exclude -- <300 files in commit scope>"
        )


# Never skip in CI: a skip that cannot fail is indistinguishable from a pass.
# (`is None`, not falsy: `CI=` empty is still CI.) See test_statusline_intensity.
@pytest.mark.skipif(
    REAL_RUFF is None and os.environ.get("CI") is None, reason="ruff not installed"
)
class TestRunRuffEndToEnd:
    def test_untracked_violation_does_not_block(
        self, git_repo: Path, ruff_calls: list[list[str]]
    ) -> None:
        (git_repo / "tracked.py").write_text("x = 1\n")
        (git_repo / "untracked.py").write_text(BAD_PY)
        _git(git_repo, "add", "tracked.py")
        assert rwg._run_ruff(git_repo, "git commit -m x") == (0, "")
        assert _linted(ruff_calls) == ["tracked.py"]

    def test_tracked_violation_blocks(self, git_repo: Path) -> None:
        (git_repo / "tracked.py").write_text(BAD_PY)
        _git(git_repo, "add", "tracked.py")
        code, output = rwg._run_ruff(git_repo, "git commit -m x")
        assert code != 0
        assert "F401" in output

    def test_violation_added_by_the_same_command_blocks(self, git_repo: Path) -> None:
        (git_repo / "new.py").write_text(BAD_PY)
        code, output = rwg._run_ruff(git_repo, "git add new.py && git commit -m x")
        assert code != 0
        assert "F401" in output

    def test_tracked_notebook_violation_blocks(self, git_repo: Path) -> None:
        # CI's `ruff check .` lints notebooks; a *.py-only scope missed them.
        (git_repo / "nb.ipynb").write_text(
            json.dumps(
                {
                    "cells": [
                        {
                            "cell_type": "code",
                            "execution_count": None,
                            "metadata": {},
                            "outputs": [],
                            "source": [BAD_PY],
                        }
                    ],
                    "metadata": {"language_info": {"name": "python"}},
                    "nbformat": 4,
                    "nbformat_minor": 5,
                }
            )
        )
        _git(git_repo, "add", "nb.ipynb")
        code, output = rwg._run_ruff(git_repo, "git commit -m x")
        assert code != 0
        assert "F401" in output

    def test_tracked_markdown_is_left_to_ruffs_discovery(
        self, git_repo: Path, ruff_calls: list[list[str]]
    ) -> None:
        # Handed an explicit .md, `ruff format` errors instead of skipping it.
        (git_repo / "README.md").write_text("# readme\n")
        (git_repo / "ok.py").write_text("x = 1\n")
        _git(git_repo, "add", "README.md", "ok.py")
        assert rwg._run_ruff(git_repo, "git commit -m x") == (0, "")
        assert _linted(ruff_calls) == ["ok.py"]

    def test_repo_exclude_is_honoured(
        self, git_repo: Path, ruff_calls: list[list[str]]
    ) -> None:
        (git_repo / "pyproject.toml").write_text('[tool.ruff]\nexclude = ["skip.py"]\n')
        (git_repo / "skip.py").write_text(BAD_PY)
        (git_repo / "ok.py").write_text("x = 1\n")
        _git(git_repo, "add", "pyproject.toml", "skip.py", "ok.py")
        assert rwg._run_ruff(git_repo, "git commit -m x") == (0, "")
        assert "skip.py" not in _linted(ruff_calls)
        assert "ok.py" in _linted(ruff_calls)

    def test_option_shaped_filename_cannot_silence_the_guard(
        self, git_repo: Path
    ) -> None:
        # Without `--` ruff read this name as a flag and linted nothing.
        (git_repo / '--config=extend-exclude=["*"] #.py').write_text("x = 1\n")
        (git_repo / "bad.py").write_text(BAD_PY)
        _git(git_repo, "add", "--", '--config=extend-exclude=["*"] #.py', "bad.py")
        code, output = rwg._run_ruff(git_repo, "git commit -m x")
        assert code != 0
        assert "F401" in output


@pytest.mark.skipif(
    REAL_RUFF is None and os.environ.get("CI") is None, reason="ruff not installed"
)
class TestMainEndToEnd:
    @pytest.fixture
    def workflow_repo(self, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        (git_repo / ".github" / "workflows").mkdir(parents=True)
        (git_repo / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n")
        _git(git_repo, "add", "pyproject.toml")
        monkeypatch.chdir(git_repo)
        return git_repo

    def _main(self, monkeypatch: pytest.MonkeyPatch, command: str) -> tuple[int, str]:
        payload = {"tool_name": "Bash", "tool_input": {"command": command}}
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        return rwg.main(), sys.stderr.getvalue()  # type: ignore[attr-defined]

    def test_tracked_violation_exits_2(
        self, workflow_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (workflow_repo / "bad.py").write_text(BAD_PY)
        _git(workflow_repo, "add", "bad.py")
        rc, err = self._main(monkeypatch, "git commit -m x")
        assert rc == 2
        assert "F401" in err

    def test_untracked_violation_exits_0(
        self,
        workflow_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        ruff_calls: list[list[str]],
    ) -> None:
        (workflow_repo / "ok.py").write_text("x = 1\n")
        _git(workflow_repo, "add", "ok.py")
        (workflow_repo / "scratch.py").write_text(BAD_PY)
        rc, _ = self._main(monkeypatch, "git commit -m x")
        assert rc == 0
        assert "scratch.py" not in _linted(ruff_calls)

    def test_block_message_names_fixes_for_the_reported_files(
        self, workflow_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `git ls-files | xargs ruff` would feed ruff .yml/.md files and fail.
        (workflow_repo / "bad.py").write_text(BAD_PY)
        _git(workflow_repo, "add", "bad.py")
        _rc, err = self._main(monkeypatch, "git commit -m x")
        assert "ruff check --fix -- <file>" in err
        assert "ls-files" not in err

    def test_cd_then_commit_does_not_lint_every_untracked_file(
        self,
        workflow_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        ruff_calls: list[list[str]],
    ) -> None:
        # The old lexer gave up on any `cd` and linted every untracked file.
        (workflow_repo / "ok.py").write_text("x = 1\n")
        _git(workflow_repo, "add", "ok.py")
        (workflow_repo / "scratch.py").write_text(BAD_PY)
        rc, _ = self._main(monkeypatch, f"cd {workflow_repo} && git commit -m x")
        assert rc == 0
        assert "scratch.py" not in _linted(ruff_calls)

    def test_commit_in_another_repo_is_checked_there(
        self, workflow_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (workflow_repo / "bad.py").write_text(BAD_PY)
        _git(workflow_repo, "add", "bad.py")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        rc, err = self._main(monkeypatch, f"git -C {workflow_repo} commit -m x")
        assert rc == 2, err

    @pytest.mark.parametrize(
        "how", ["session-in-subdir", "cd-sub", "git-dash-C-sub", "cd-sub-add"]
    )
    def test_commit_from_a_subdirectory_is_checked_at_the_root(
        self, workflow_repo: Path, monkeypatch: pytest.MonkeyPatch, how: str
    ) -> None:
        # Workflows and pyproject.toml live at the root; a commit run from a
        # subdirectory used to skip the gate silently.
        (workflow_repo / "pkg").mkdir()
        (workflow_repo / "pkg" / "bad.py").write_text(BAD_PY)
        command = {
            "session-in-subdir": "git commit -m x",
            "cd-sub": "cd pkg && git commit -m x",
            "git-dash-C-sub": "git -C pkg commit -m x",
            "cd-sub-add": "cd pkg && git add bad.py && git commit -m x",
        }[how]
        if how != "cd-sub-add":
            _git(workflow_repo, "add", "pkg/bad.py")
        if how == "session-in-subdir":
            monkeypatch.chdir(workflow_repo / "pkg")
        rc, err = self._main(monkeypatch, command)
        assert rc == 2, err
        assert "F401" in err

    def test_commit_into_a_plain_repo_is_not_gated(
        self, workflow_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The reverse of test_commit_in_another_repo_is_checked_there: the
        # session sits in the workflow repo, the commit lands elsewhere.
        (workflow_repo / "bad.py").write_text(BAD_PY)
        _git(workflow_repo, "add", "bad.py")
        plain = tmp_path / "plain"
        plain.mkdir()
        subprocess.run(["git", "init", "-q", str(plain)], check=True)
        rc, err = self._main(monkeypatch, f"git -C {plain} commit -m x")
        assert rc == 0, err

    def test_payload_cwd_is_where_the_session_is(
        self,
        workflow_repo: Path,
        tmp_path_factory: pytest.TempPathFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The hook process may run elsewhere; the payload's cwd follows the
        # session's own `cd`s. `elsewhere` must be OUTSIDE the repo
        # (git_repo is tmp_path itself).
        (workflow_repo / "bad.py").write_text(BAD_PY)
        _git(workflow_repo, "add", "bad.py")
        elsewhere = tmp_path_factory.mktemp("outside")
        monkeypatch.chdir(elsewhere)
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "git commit -m x"},
            "cwd": str(workflow_repo),
        }
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        assert rwg.main() == 2

    def test_symlinked_cwd_rebases_pathspecs_inside_the_repo(
        self, workflow_repo: Path, tmp_path: Path
    ) -> None:
        (workflow_repo / "pkg").mkdir()
        link = tmp_path / "link-to-pkg"
        link.symlink_to(workflow_repo / "pkg")
        target = rwg._commit_target("git add new.py && git commit -m x", link)
        assert target is not None
        assert target[1] == ["pkg/new.py"]

    def test_non_repo_session_cwd_is_not_gated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        rc, _ = self._main(monkeypatch, "git commit -m x")
        assert rc == 0
