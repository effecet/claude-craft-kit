"""Tests for session_start.py's hook_drift check.

Why this check exists: on a machine where ~/.claude/hooks holds real file COPIES,
nothing else compares them to the config checkout, so a deployed copy can be
edited into a local fork — and the next copy step silently reverts it.

The check earns its place by reporting DIRECTION, not just inequality. "These
files differ" is not actionable; "the deployed copy is AHEAD of the repo" means
*do not copy over this file, it is a fork* and is the whole point.

These tests build both trees from scratch under tmp_path. They never read the
operator's real ~/.claude/hooks — a test that did would pass or fail based on
whichever machine ran it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

import session_start as ss  # noqa: E402


@pytest.fixture(autouse=True)
def _upstream_style_layout(monkeypatch):
    """These fixtures build `<repo>/claude-config/{hooks,scripts}`, so point the
    CRAFT_CONFIG_SUBDIR setting at it (the kit's own default is the root)."""
    monkeypatch.setattr(ss, "CONFIG_SUBDIR", "claude-config")


@pytest.fixture
def trees(tmp_path):
    """Return (repo, deployed) — two empty hook directories."""
    repo, deployed = tmp_path / "repo", tmp_path / "deployed"
    repo.mkdir()
    deployed.mkdir()
    return repo, deployed


def _put(d: Path, name: str, body: str) -> None:
    (d / name).write_text(body)


def _check(repo: Path, deployed: Path) -> list[str]:
    return ss.hook_drift(deployed_dir=deployed, repo_hooks=repo)


# ── the silent cases: this must not cry wolf ────────────────────────────────


class TestSilentWhenNothingIsWrong:
    def test_identical_files_report_nothing(self, trees):
        repo, deployed = trees
        _put(repo, "a.py", "x\ny\n")
        _put(deployed, "a.py", "x\ny\n")
        assert _check(repo, deployed) == []

    def test_same_directory_reports_nothing(self, trees):
        """A symlinked setup: ~/.claude/hooks is a SYMLINK into claude-config/hooks.

        Both paths resolve to one directory, so there is nothing to compare.
        A check that fired here would be noise on every session start.
        """
        repo, _ = trees
        _put(repo, "a.py", "x\n")
        assert ss.hook_drift(deployed_dir=repo, repo_hooks=repo) == []

    def test_missing_deployed_dir_is_silent(self, trees):
        """A CI checkout has no ~/.claude/hooks. Never fail a session start."""
        repo, _ = trees
        _put(repo, "a.py", "x\n")
        assert ss.hook_drift(deployed_dir=repo / "nope", repo_hooks=repo) == []

    def test_missing_repo_dir_is_silent(self, trees):
        repo, deployed = trees
        _put(deployed, "a.py", "x\n")
        assert ss.hook_drift(deployed_dir=deployed, repo_hooks=repo / "nope") == []

    def test_file_not_deployed_is_not_drift(self, trees):
        """Hook sets differ per machine. An undeployed hook is a choice."""
        repo, deployed = trees
        _put(repo, "mac_only.py", "x\n")
        assert _check(repo, deployed) == []

    # test_non_python_files_ignored was REMOVED. It asserted that a
    # drifting .sh was silently ignored — it encoded the bug rather than a
    # requirement. Its replacement is
    # TestShellScriptsAreScanned::test_shell_script_drift_is_reported, which
    # asserts the opposite. Left as a note so the deletion is not mistaken for
    # lost coverage.


# ── direction: the reason this check is worth having ────────────────────────


class TestReportsDirection:
    def test_deployed_ahead_is_named_as_a_fork(self, trees):
        """The destructive case — copying over this REVERTS working code."""
        repo, deployed = trees
        _put(repo, "stop_gate.py", "a\n")
        _put(deployed, "stop_gate.py", "a\nlocal_fix\n")

        (msg,) = _check(repo, deployed)

        assert "AHEAD of repo" in msg
        assert "stop_gate.py" in msg, "a fork must be NAMED — it needs per-file triage"
        assert "upstream before copying" in msg

    def test_deployed_behind_is_merely_stale(self, trees):
        repo, deployed = trees
        _put(repo, "a.py", "a\nnew_upstream\n")
        _put(deployed, "a.py", "a\n")

        (msg,) = _check(repo, deployed)

        assert "stale" in msg
        assert "AHEAD" not in msg
        assert "safe to re-copy" in msg, "a pure subset is safe to overwrite"

    def test_both_sides_unique_is_diverged(self, trees):
        repo, deployed = trees
        _put(repo, "a.py", "shared\nfrom_repo\n")
        _put(deployed, "a.py", "shared\nfrom_pi\n")

        (msg,) = _check(repo, deployed)

        assert "diverged" in msg
        assert "a.py" in msg
        assert "upstream before copying" in msg

    def test_a_fork_alongside_stale_files_still_says_upstream(self, trees):
        """The mixed case, which is what a copy-deployed machine actually looks like.

        If any file is a fork, the whole message must NOT say "safe to
        re-copy" — one blanket copy would revert it.
        """
        repo, deployed = trees
        _put(repo, "fork.py", "a\n")
        _put(deployed, "fork.py", "a\npi_only\n")
        _put(repo, "stale.py", "a\nb\n")
        _put(deployed, "stale.py", "a\n")

        (msg,) = _check(repo, deployed)

        assert "AHEAD of repo (fork.py)" in msg
        assert "1 stale" in msg
        assert "safe to re-copy" not in msg


class TestReordering:
    def test_pure_reordering_is_not_reported_as_a_fork(self, trees):
        """Direction comes from set difference, not line-by-line diff.

        A reindent or a reordered import block has no unique lines on either
        side. Nothing unique is lost by overwriting it, so there is nothing to
        act on and the check stays quiet. Documented blind spot: a file whose
        only change is a DUPLICATED existing line is also silent.
        """
        repo, deployed = trees
        _put(repo, "a.py", "import os\nimport sys\n")
        _put(deployed, "a.py", "import sys\nimport os\n")
        assert _check(repo, deployed) == []


# ── the excluded permanent forks ────────────────────────────────────────────


class TestPermanentForksAreExcluded:
    def test_a_configured_permanent_fork_is_never_reported(self, trees, monkeypatch):
        """A file listed as a PERMANENT per-machine fork (CRAFT_HOOK_DRIFT_EXCLUDE).

        Reporting it would fire on every single session start forever, which
        trains the operator to ignore the whole line — and then the one real
        fork goes unread too.
        """
        monkeypatch.setattr(ss, "HOOK_DRIFT_EXCLUDE", ss.HOOK_DRIFT_EXCLUDE | {"mcp_restore_guard.sh"})
        repo, deployed = trees
        _put(repo, "mcp_restore_guard.sh", "machine-a\n")
        _put(deployed, "mcp_restore_guard.sh", "machine-b\n")
        assert _check(repo, deployed) == []

    def test_exclusion_list_does_not_hide_a_real_hook(self, trees):
        """Guard against someone widening the exclusions into uselessness."""
        repo, deployed = trees
        _put(repo, "stop_gate.py", "a\n")
        _put(deployed, "stop_gate.py", "a\nfork\n")
        assert "stop_gate.py" not in ss.HOOK_DRIFT_EXCLUDE
        assert _check(repo, deployed) != []


# ── never break session start ───────────────────────────────────────────────


class TestFailsSafe:
    def test_unreadable_file_is_skipped_not_fatal(self, trees):
        repo, deployed = trees
        _put(repo, "ok.py", "a\n")
        _put(deployed, "ok.py", "a\n")
        (repo / "binary.py").write_bytes(b"\xff\xfe\x00garbage")
        _put(deployed, "binary.py", "text\n")

        assert _check(repo, deployed) == []

    def test_internal_error_returns_empty_and_warns(self, trees, monkeypatch, capsys):
        """Silent in the brief, never silent everywhere.

        A crash must not be indistinguishable from "no drift" — that is the
        exact failure mode this whole check was built to end.
        """
        repo, deployed = trees

        def boom(*a, **k):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(ss.Path, "is_dir", boom)

        assert ss.hook_drift(deployed_dir=deployed, repo_hooks=repo) == []
        assert "hook_drift failed" in capsys.readouterr().err


# ── the target set must be data-driven, not one hardcoded dir pair ─────────
#
# Scanning only `hooks/*.py` against ~/.claude/hooks misses two kinds of
# target: a `.sh` deploy target, and any destination outside that one
# directory (a script settings.json runs from somewhere else entirely). A
# guard covering 9 of 10 surfaces teaches the operator the surface is
# covered. These tests pin the tenth.


def _settings(path: Path, *commands: str) -> Path:
    """Write a minimal settings.json whose hooks invoke `commands`."""
    import json

    path.write_text(
        json.dumps(
            {"hooks": {"SessionStart": [{"hooks": [{"command": c} for c in commands]}]}}
        )
    )
    return path


class TestShellScriptsAreScanned:
    """`.sh` files deploy exactly like `.py` files and rot exactly as quietly."""

    def test_shell_script_drift_is_reported(self, trees):
        repo, deployed = trees
        _put(repo, "on_idle.sh", "a\n")
        _put(deployed, "on_idle.sh", "a\npi_only\n")

        (msg,) = _check(repo, deployed)

        assert "AHEAD of repo" in msg
        assert "on_idle.sh" in msg

    def test_excluded_shell_fork_is_still_never_reported(self, trees, monkeypatch):
        """Scanning .sh must not start reporting a configured PERMANENT fork."""
        monkeypatch.setattr(ss, "HOOK_DRIFT_EXCLUDE", ss.HOOK_DRIFT_EXCLUDE | {"mcp_restore_guard.sh"})
        repo, deployed = trees
        _put(repo, "mcp_restore_guard.sh", "machine-a\n")
        _put(deployed, "mcp_restore_guard.sh", "machine-b\n")
        assert _check(repo, deployed) == []


class TestDiscoveredTargets:
    """Destinations come from the machine, not from a hardcoded pair."""

    def test_settings_derived_destination_outside_hooks_is_scanned(self, tmp_path):
        """A copy-deployed machine: a script deployed to a dir this check never knew about.

        The repo copy lives in claude-config/scripts/; the live settings.json
        invokes it from somewhere else entirely (a scripts dir outside
        ~/.claude). Only the live settings file knows that path.
        """
        repo_root = tmp_path / "repo"
        (repo_root / "claude-config" / "hooks").mkdir(parents=True)
        (repo_root / "claude-config" / "scripts").mkdir(parents=True)
        (repo_root / "claude-config" / "scripts" / "probe.sh").write_text("new\n")

        elsewhere = tmp_path / "other-checkout" / "scripts"
        elsewhere.mkdir(parents=True)
        (elsewhere / "probe.sh").write_text("new\nstale_local\n")

        claude_dir = tmp_path / "dotclaude"
        (claude_dir / "hooks").mkdir(parents=True)
        settings = _settings(tmp_path / "settings.json", f"bash {elsewhere}/probe.sh")

        (msg,) = ss.hook_drift(
            repo_root=repo_root, claude_dir=claude_dir, settings_path=settings
        )
        assert "probe.sh" in msg

    def test_invoked_path_with_no_repo_counterpart_is_ignored(self, tmp_path, capsys):
        """settings.json invokes unrelated project scripts too.

        A real settings.json can invoke something like
        ~/Downloads/some-project/scripts/extract_pdf.py, which has no
        counterpart in the config checkout at all. Comparing it against nothing must
        not crash or report.
        """
        repo_root = tmp_path / "repo"
        (repo_root / "claude-config" / "hooks").mkdir(parents=True)
        (repo_root / "claude-config" / "scripts").mkdir(parents=True)

        foreign = tmp_path / "other-project" / "scripts"
        foreign.mkdir(parents=True)
        (foreign / "extract_pdf.py").write_text("unrelated\n")

        claude_dir = tmp_path / "dotclaude"
        (claude_dir / "hooks").mkdir(parents=True)
        settings = _settings(
            tmp_path / "settings.json", f"python3 {foreign}/extract_pdf.py"
        )

        assert (
            ss.hook_drift(
                repo_root=repo_root, claude_dir=claude_dir, settings_path=settings
            )
            == []
        )
        # An empty list is also what a CRASH returns. Without this the test
        # passes when the counterpart lookup raises and the outer except
        # swallows it — a blanket except making a bug look like a clean result.
        assert "hook_drift failed" not in capsys.readouterr().err

    def test_colliding_basename_outside_a_deploy_dir_is_ignored(self, tmp_path):
        """Basename matching alone would invent drift.

        A foreign path whose filename happens to match one of ours is not a
        deploy of ours. Compared anyway, it reports drift the operator cannot
        act on — and this check's whole value is that its silence and its noise
        both mean something.
        """
        repo_root = tmp_path / "repo"
        repo_scripts = repo_root / "claude-config" / "scripts"
        (repo_root / "claude-config" / "hooks").mkdir(parents=True)
        repo_scripts.mkdir(parents=True)
        (repo_scripts / "probe.sh").write_text("ours\n")

        # Same filename, unrelated project, NOT in a hooks/ or scripts/ dir.
        foreign = tmp_path / "other-project" / "lib"
        foreign.mkdir(parents=True)
        (foreign / "probe.sh").write_text("theirs\nentirely\n")

        claude_dir = tmp_path / "dotclaude"
        (claude_dir / "hooks").mkdir(parents=True)
        settings = _settings(tmp_path / "settings.json", f"bash {foreign}/probe.sh")

        assert (
            ss.hook_drift(
                repo_root=repo_root, claude_dir=claude_dir, settings_path=settings
            )
            == []
        )

    def test_symlinked_hooks_dir_does_not_disable_other_targets(self, tmp_path):
        """A symlinked setup: ~/.claude/hooks is a SYMLINK, ~/.claude/scripts is REAL.

        The pre-short-circuit returned [] for the WHOLE check as soon as
        the hooks pair resolved to one directory — which on a symlinked setup is always.
        Left per-check rather than per-pair, every target added by would be
        silently dead on the one machine being used to develop it.
        """
        repo_root = tmp_path / "repo"
        repo_hooks = repo_root / "claude-config" / "hooks"
        repo_scripts = repo_root / "claude-config" / "scripts"
        repo_hooks.mkdir(parents=True)
        repo_scripts.mkdir(parents=True)
        (repo_hooks / "track.py").write_text("same\n")
        (repo_scripts / "probe.sh").write_text("a\n")

        claude_dir = tmp_path / "dotclaude"
        claude_dir.mkdir()
        (claude_dir / "hooks").symlink_to(repo_hooks)  # a symlinked hooks dir
        (claude_dir / "scripts").mkdir()
        (claude_dir / "scripts" / "probe.sh").write_text("a\nlocal_fork\n")

        settings = _settings(tmp_path / "settings.json")

        (msg,) = ss.hook_drift(
            repo_root=repo_root, claude_dir=claude_dir, settings_path=settings
        )
        assert "probe.sh" in msg, "symlinked hooks dir must not kill scripts coverage"

    def test_missing_settings_file_degrades_to_static_floor(self, tmp_path):
        """No settings.json must not mean no check at all."""
        repo_root = tmp_path / "repo"
        repo_scripts = repo_root / "claude-config" / "scripts"
        (repo_root / "claude-config" / "hooks").mkdir(parents=True)
        repo_scripts.mkdir(parents=True)
        (repo_scripts / "probe.sh").write_text("a\n")

        claude_dir = tmp_path / "dotclaude"
        (claude_dir / "hooks").mkdir(parents=True)
        (claude_dir / "scripts").mkdir()
        (claude_dir / "scripts" / "probe.sh").write_text("a\nfork\n")

        (msg,) = ss.hook_drift(
            repo_root=repo_root,
            claude_dir=claude_dir,
            settings_path=tmp_path / "absent.json",
        )
        assert "probe.sh" in msg

    def test_malformed_settings_file_degrades_to_static_floor(self, tmp_path):
        repo_root = tmp_path / "repo"
        repo_scripts = repo_root / "claude-config" / "scripts"
        (repo_root / "claude-config" / "hooks").mkdir(parents=True)
        repo_scripts.mkdir(parents=True)
        (repo_scripts / "probe.sh").write_text("a\n")

        claude_dir = tmp_path / "dotclaude"
        (claude_dir / "hooks").mkdir(parents=True)
        (claude_dir / "scripts").mkdir()
        (claude_dir / "scripts" / "probe.sh").write_text("a\nfork\n")

        bad = tmp_path / "settings.json"
        bad.write_text("{ not json at all")

        (msg,) = ss.hook_drift(
            repo_root=repo_root, claude_dir=claude_dir, settings_path=bad
        )
        assert "probe.sh" in msg


class TestCoverageIsStated:
    """Silence must mean "nothing drifted", not "nothing was looked at"."""

    def test_alert_line_states_what_was_scanned(self, trees):
        repo, deployed = trees
        _put(repo, "a.py", "x\n")
        _put(deployed, "a.py", "x\nfork\n")

        (msg,) = _check(repo, deployed)

        assert "scanned" in msg.lower()


# ── gate round 2: gaps the reviewers proved were unpinned ────────────────────
#
# A mutation harness found 9 surviving mutations after the first round. These
# close the ones that matter. The largest was that NO test exercised the `~`
# expansion branch — measured against the real ~/.claude/settings.json, 21 of
# 23 discovered targets flow through it, including the probe script this whole
# issue exists for. Breaking it would silently revert discovery to the static
# floor while the alert kept claiming coverage.


def _tree(tmp_path):
    """(repo_root, claude_dir) with both source dirs present."""
    repo_root = tmp_path / "repo"
    (repo_root / "claude-config" / "hooks").mkdir(parents=True)
    (repo_root / "claude-config" / "scripts").mkdir(parents=True)
    claude_dir = tmp_path / "dotclaude"
    (claude_dir / "hooks").mkdir(parents=True)
    (claude_dir / "scripts").mkdir(parents=True)
    return repo_root, claude_dir


class TestHomeExpansion:
    def test_tilde_path_in_settings_is_expanded_and_scanned(self, tmp_path):
        """21 of 23 real targets take this branch. Nothing covered it.

        The deployed copy sits in ~/brain/scripts — OUTSIDE ~/.claude, so the
        static floor cannot reach it. A first version of this test deployed
        into ~/.claude/scripts, where the floor reported the same drift and the
        tilde branch could be deleted with the test still green.
        """
        repo_root, claude_dir = _tree(tmp_path)
        (repo_root / "claude-config" / "scripts" / "probe.sh").write_text("a\n")
        elsewhere = tmp_path / "brain" / "scripts"
        elsewhere.mkdir(parents=True)
        (elsewhere / "probe.sh").write_text("a\nlocal\n")

        # home is claude_dir.parent, so "~" here means tmp_path.
        settings = _settings(tmp_path / "s.json", "bash ~/brain/scripts/probe.sh")

        (msg,) = ss.hook_drift(
            repo_root=repo_root, claude_dir=claude_dir, settings_path=settings
        )
        assert "probe.sh" in msg

    def test_dollar_home_is_not_matched_as_a_prefix(self, tmp_path, monkeypatch):
        """$HOMEBREW_PREFIX must not be rewritten as $HOME + 'BREW_PREFIX'.

        Asserting only `is None` was vacuous: the naive form produces a sibling
        of home, which the escape-home guard rejects anyway. Pointing the var
        at a real directory UNDER home makes the correct expansion resolve and
        the naive one fail, so this pins the substitution itself.
        """
        brew = tmp_path / "brew"
        brew.mkdir()
        monkeypatch.setenv("HOMEBREW_PREFIX", str(brew))
        got = ss._expand_home("$HOMEBREW_PREFIX/scripts/x.py", tmp_path)
        assert got == brew / "scripts" / "x.py"

    def test_named_tilde_user_is_not_expanded(self, tmp_path):
        assert ss._expand_home("~someone/scripts/x.py", tmp_path) is None

    def test_relative_token_is_skipped(self, tmp_path, monkeypatch):
        """Relative to a CWD this process does not share — cannot be placed.

        CWD is set to home so the escape-home guard would ACCEPT the resolved
        path; only the explicit relative check can reject it. Without this the
        test passed for the wrong reason.
        """
        monkeypatch.chdir(tmp_path)
        assert ss._expand_home("hooks/track.py", tmp_path) is None

    def test_path_escaping_home_is_skipped(self, tmp_path):
        """`..` must not walk the scan outside the deploy tree."""
        assert ss._expand_home("$HOME/../../etc/hooks/track.py", tmp_path) is None


class TestScanIsBounded:
    def test_file_with_other_suffix_is_not_scanned(self, trees):
        """Restores the bound lost when test_non_python_files_ignored went.

        That test asserted a drifting .sh was ignored, which encoded the bug —
        but it also pinned that the scan is BOUNDED, and deleting it left the
        suffix filter unpinned in both call sites.
        """
        repo, deployed = trees
        _put(repo, "notes.txt", "one\n")
        _put(deployed, "notes.txt", "two\n")
        _put(repo, "config.json", "{}\n")
        _put(deployed, "config.json", '{"a":1}\n')
        assert _check(repo, deployed) == []

    def test_other_suffix_is_not_scanned_in_discovery_mode(self, tmp_path):
        """The suffix filter exists at TWO call sites; the explicit-pair test
        only covered one, so the discovery-mode filter was unpinned."""
        repo_root, claude_dir = _tree(tmp_path)
        (repo_root / "claude-config" / "scripts" / "notes.txt").write_text("one\n")
        (claude_dir / "scripts" / "notes.txt").write_text("two\n")
        settings = _settings(tmp_path / "s.json")
        assert (
            ss.hook_drift(
                repo_root=repo_root, claude_dir=claude_dir, settings_path=settings
            )
            == []
        )


class TestBasenameCollision:
    def test_same_name_in_both_source_dirs_is_attributed_by_deploy_dir(
        self, tmp_path
    ):
        """A bare basename index reported an in-sync file as diverged.

        Reproduced by the bug-hunter: foo.py in BOTH claude-config/hooks and
        claude-config/scripts, deployed copy byte-identical to the scripts one.
        Keyed on basename alone the hooks copy won, and the check reported
        `1 diverged (foo.py) — upstream before copying` on a file in sync.
        """
        repo_root, claude_dir = _tree(tmp_path)
        (repo_root / "claude-config" / "hooks" / "foo.py").write_text("HOOKS\n")
        (repo_root / "claude-config" / "scripts" / "foo.py").write_text("SCRIPTS\n")

        elsewhere = tmp_path / "brain" / "scripts"
        elsewhere.mkdir(parents=True)
        (elsewhere / "foo.py").write_text("SCRIPTS\n")  # matches the scripts copy

        settings = _settings(tmp_path / "s.json", f"python3 {elsewhere}/foo.py")

        out = ss.hook_drift(
            repo_root=repo_root, claude_dir=claude_dir, settings_path=settings
        )
        assert out == [], f"in-sync file reported as drift: {out}"


class TestDeduplication:
    def test_a_target_reached_by_both_sources_is_named_once(self, tmp_path):
        """~/.claude/hooks/track.py is produced by the floor AND by settings."""
        repo_root, claude_dir = _tree(tmp_path)
        (repo_root / "claude-config" / "hooks" / "track.py").write_text("a\n")
        (claude_dir / "hooks" / "track.py").write_text("a\nfork\n")

        settings = _settings(tmp_path / "s.json", "python3 ~/dotclaude/hooks/track.py")

        (msg,) = ss.hook_drift(
            repo_root=repo_root, claude_dir=claude_dir, settings_path=settings
        )
        assert msg.count("track.py") == 1, f"duplicate pair leaked into: {msg}"


class TestCrossDirectoryDeploy:
    def test_hooks_file_installed_into_scripts_is_compared(self, tmp_path):
        """An install step can copy a hook into ~/.claude/scripts/ (or the reverse).

        Pairing only like-named subdirs never compares such a file, so a stale
        copy there would keep running while the check reports healthy.
        """
        repo_root, claude_dir = _tree(tmp_path)
        (repo_root / "claude-config" / "hooks" / "helper.py").write_text("new\n")
        (claude_dir / "scripts" / "helper.py").write_text("new\nstale_extra\n")

        settings = _settings(tmp_path / "s.json")

        (msg,) = ss.hook_drift(
            repo_root=repo_root, claude_dir=claude_dir, settings_path=settings
        )
        assert "helper.py" in msg


class TestUnreadableSettings:
    def test_non_utf8_settings_still_leaves_the_static_floor(self, tmp_path):
        """UnicodeDecodeError is a ValueError, not an OSError.

        Uncaught it reached the outer handler and disabled the WHOLE check
        while the code claimed the static floor still applied.
        """
        repo_root, claude_dir = _tree(tmp_path)
        (repo_root / "claude-config" / "scripts" / "probe.sh").write_text("a\n")
        (claude_dir / "scripts" / "probe.sh").write_text("a\nfork\n")

        bad = tmp_path / "s.json"
        bad.write_bytes(b"\xff\xfe\x00 not utf-8 at all")

        (msg,) = ss.hook_drift(
            repo_root=repo_root, claude_dir=claude_dir, settings_path=bad
        )
        assert "probe.sh" in msg


class TestModeSelection:
    def test_mixing_both_argument_families_raises(self, tmp_path):
        """Silently discarding one family compared something else entirely."""
        with pytest.raises(TypeError):
            ss.hook_drift(deployed_dir=tmp_path, repo_root=tmp_path)

    def test_empty_string_does_not_silently_scan_the_live_machine(
        self, tmp_path, monkeypatch
    ):
        """Mode chosen with `is not None`, value bound with truthiness.

        hook_drift(deployed_dir="") entered explicit mode and then fell back to
        the real CLAUDE_DIR/hooks — a call reading as "compare nothing" quietly
        compared the operator's actual machine.
        """
        # Path("") is Path("."), so this scans the CWD. Anchor it at an empty
        # directory: without this the suite passes from the repo root and FAILS
        # from claude-config/hooks, where "." is full of real hooks.
        empty = tmp_path / "cwd"
        empty.mkdir()
        monkeypatch.chdir(empty)

        fake_machine = tmp_path / "live" / "hooks"
        fake_machine.mkdir(parents=True)
        (fake_machine / "track.py").write_text("a\nlocal_fork\n")
        monkeypatch.setattr(ss, "CLAUDE_DIR", tmp_path / "live")

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "track.py").write_text("a\n")

        # Truthiness binding falls back to CLAUDE_DIR/hooks and reports the
        # fork; `is not None` compares the empty string and finds nothing.
        assert ss.hook_drift(deployed_dir="", repo_hooks=repo) == []


class TestCoverageNumbers:
    def test_counts_are_accurate_not_just_present(self, tmp_path):
        """Asserting only the word "scanned" let 0-of-0 pass alongside a fork.

        Deliberately a fixture where compared != targets and destinations > 1:
        with 2-of-2-across-1 every count collapses to the same number and a
        mutation swapping them stayed green.

        a.py pairs to hooks/ and b.sh to scripts/ — 2 targets across 2
        destinations, of which only ~/.claude/hooks/a.py exists and is
        compared. compared != targets and destinations > 1, so a mutation
        swapping any of the three is visible.
        """
        repo_root, claude_dir = _tree(tmp_path)
        (repo_root / "claude-config" / "hooks" / "a.py").write_text("x\n")
        (repo_root / "claude-config" / "scripts" / "b.sh").write_text("same\n")
        (claude_dir / "hooks" / "a.py").write_text("x\nfork\n")
        settings = _settings(tmp_path / "s.json")

        (msg,) = ss.hook_drift(
            repo_root=repo_root, claude_dir=claude_dir, settings_path=settings
        )
        assert "Scanned 1 of 2 target(s) across 2 destination(s)" in msg


# ── gate round 3: mutations that survived the scoped re-run ──────────────────


class TestSurvivingMutations:
    """Each pins a behaviour a reviewer proved could be deleted silently."""

    def test_same_file_pair_is_skipped_not_compared(self, tmp_path):
        """M5: deleting the resolve/samefile skip left every test green.

        Contents are identical either way, so no drift appears — the skip is
        observable only in the reported counts. A symlinked deploy dir must
        contribute targets but zero comparisons.
        """
        repo_root, claude_dir = _tree(tmp_path)
        repo_hooks = repo_root / "claude-config" / "hooks"
        (repo_hooks / "track.py").write_text("same\n")
        (repo_root / "claude-config" / "scripts" / "drift.sh").write_text("a\n")
        (claude_dir / "scripts" / "drift.sh").write_text("a\nfork\n")

        # Replace the plain hooks dir with a symlink to the repo's, as on a symlinked setup.
        import shutil

        shutil.rmtree(claude_dir / "hooks")
        (claude_dir / "hooks").symlink_to(repo_hooks)

        (msg,) = ss.hook_drift(
            repo_root=repo_root,
            claude_dir=claude_dir,
            settings_path=_settings(tmp_path / "s.json"),
        )
        # track.py is a target and is NOT compared; drift.sh is both.
        assert "Scanned 1 of 2 target(s)" in msg

    def test_settings_paths_are_not_resolved_through_symlinks(self, tmp_path):
        """M13: returning candidate.resolve() re-created the self-pair bug.

        Resolving follows a symlinked hooks dir back into the repo, so
        the pair becomes (repo_file, repo_file) under a third "destination"
        that is really the source.
        """
        repo_root, claude_dir = _tree(tmp_path)
        repo_hooks = repo_root / "claude-config" / "hooks"
        (repo_hooks / "track.py").write_text("a\n")
        (repo_root / "claude-config" / "scripts" / "drift.sh").write_text("a\n")
        (claude_dir / "scripts" / "drift.sh").write_text("a\nfork\n")

        import shutil

        shutil.rmtree(claude_dir / "hooks")
        (claude_dir / "hooks").symlink_to(repo_hooks)

        settings = _settings(tmp_path / "s.json", "python3 ~/dotclaude/hooks/track.py")

        (msg,) = ss.hook_drift(
            repo_root=repo_root, claude_dir=claude_dir, settings_path=settings
        )
        # Resolving would add the repo dir as a third destination.
        assert "across 2 destination(s)" in msg

    def test_ambiguous_basename_is_not_cross_paired(self, tmp_path):
        """M6: cross-dir pairing must respect the ambiguity guard.

        foo.py exists in BOTH source dirs, so a deployed ~/.claude/scripts/foo.py
        cannot be attributed to one of them. Cross-pairing anyway picks a source
        arbitrarily and reports drift against the wrong file.
        """
        repo_root, claude_dir = _tree(tmp_path)
        (repo_root / "claude-config" / "hooks" / "foo.py").write_text("HOOKS\n")
        (repo_root / "claude-config" / "scripts" / "foo.py").write_text("SCRIPTS\n")
        # Deployed copy matches the scripts source exactly.
        (claude_dir / "scripts" / "foo.py").write_text("SCRIPTS\n")

        assert (
            ss.hook_drift(
                repo_root=repo_root,
                claude_dir=claude_dir,
                settings_path=_settings(tmp_path / "s.json"),
            )
            == []
        )

    def test_braced_env_var_is_expanded(self, tmp_path, monkeypatch):
        """M1: dropping the ${VAR} alternative from the regex."""
        target = tmp_path / "brain"
        target.mkdir()
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(target))
        got = ss._expand_home("${CLAUDE_PROJECT_DIR}/scripts/x.py", tmp_path)
        assert got == target / "scripts" / "x.py"

    def test_suffix_lookahead_rejects_longer_extensions(self, tmp_path):
        r"""M11: without (?![\w.]) a .pyc / .sh.bak matched as .py / .sh."""
        found = ss._INVOKED_PATH_RE.findall("python3 /a/hooks/track.pyc")
        assert found == []
        assert ss._INVOKED_PATH_RE.findall("bash /a/hooks/notify.sh.bak") == []
        assert ss._INVOKED_PATH_RE.findall("python3 /a/hooks/track.py") != []

    def test_bare_tilde_is_not_a_path(self, tmp_path):
        """M10: `~` alone must not expand into the home directory itself."""
        assert ss._expand_home("~", tmp_path) is None

    def test_destinations_counts_directories_not_files(self, tmp_path):
        """M19: keying destinations by file name inflated the count."""
        repo_root, claude_dir = _tree(tmp_path)
        (repo_root / "claude-config" / "hooks" / "a.py").write_text("x\n")
        (repo_root / "claude-config" / "hooks" / "b.py").write_text("y\n")
        (claude_dir / "hooks" / "a.py").write_text("x\nfork\n")
        (claude_dir / "hooks" / "b.py").write_text("y\nfork\n")

        (msg,) = ss.hook_drift(
            repo_root=repo_root,
            claude_dir=claude_dir,
            settings_path=_settings(tmp_path / "s.json"),
        )
        assert "across 1 destination(s)" in msg


class TestZeroTargetsIsLoud:
    def test_no_targets_warns_on_stderr(self, tmp_path, capsys):
        """A monitor covering nothing must not look like one reporting fine.

        _find_memory_persistor() probes only ~/Desktop and ~/Downloads, so a
        checkout anywhere else yields zero targets — previously in silence.
        """
        empty_repo = tmp_path / "norepo"
        empty_repo.mkdir()
        claude_dir = tmp_path / "dotclaude"
        (claude_dir / "hooks").mkdir(parents=True)

        assert (
            ss.hook_drift(
                repo_root=empty_repo,
                claude_dir=claude_dir,
                settings_path=_settings(tmp_path / "s.json"),
            )
            == []
        )
        assert "no targets" in capsys.readouterr().err


class TestDeployTreeOutsideHome:
    def test_symlinked_deploy_dir_pointing_outside_home_still_scans(self, tmp_path):
        """Validating the RESOLVED path dropped every settings-derived target.

        A ~/.claude/hooks that symlinks outside $HOME is a legitimate layout;
        resolving before the containment check silently zeroed coverage there,
        which is the archetype this whole check exists to prevent.
        """
        home = tmp_path / "home"
        (home / "dotclaude").mkdir(parents=True)
        outside = tmp_path / "elsewhere" / "hooks"
        outside.mkdir(parents=True)
        (home / "dotclaude" / "hooks").symlink_to(outside)

        token = "~/dotclaude/hooks/track.py"
        assert ss._expand_home(token, home) == home / "dotclaude/hooks/track.py"
