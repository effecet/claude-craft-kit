"""Tests for validate_tier.py.

Locks in the edge cases caught during Wave 3 self-check:
- Forbidden markers in code → BLOCK
- Forbidden markers in markdown prose → NOT BLOCK (skip-pattern files exempt)
- Sensitive paths with underscores (auth_session.py) → Full
- Skip / Light / Full thresholds
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Make the script importable
HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

import validate_tier as vt  # noqa: E402

SCRIPT = HOOKS_DIR / "validate_tier.py"


# ---------- pure unit tests (no git required) ----------


class TestRecommendTier:
    def test_empty_diff_is_skip(self) -> None:
        result = vt.recommend_tier(files=[], added={}, loc=0)
        assert result["tier"] == "Skip"

    def test_docs_only_is_skip(self) -> None:
        result = vt.recommend_tier(
            files=["README.md", "docs/architecture.md"],
            added={"README.md": ["# hi"], "docs/architecture.md": ["x"]},
            loc=2,
        )
        assert result["tier"] == "Skip"

    def test_marker_in_code_blocks(self) -> None:
        result = vt.recommend_tier(
            files=["src/foo.py"],
            added={"src/foo.py": ["    pass  # TODO fix later"]},
            loc=1,
        )
        assert result["tier"] == "BLOCK"
        assert any("TODO" in m for m in result["markers"])

    def test_marker_in_markdown_does_not_block(self) -> None:
        # Watching the literal word TODO in prose should not block.
        result = vt.recommend_tier(
            files=["notes.md"],
            added={"notes.md": ["Watch for TODO markers in code."]},
            loc=1,
        )
        assert result["tier"] == "Skip"

    def test_marker_in_yaml_does_not_block(self) -> None:
        result = vt.recommend_tier(
            files=["config.yaml"],
            added={"config.yaml": ["description: TODO list of items"]},
            loc=1,
        )
        assert result["tier"] == "Skip"

    def test_sensitive_path_with_underscore(self) -> None:
        # Regression: \bauth\b failed because _ is a word char.
        # auth_session.py must be detected as sensitive.
        result = vt.recommend_tier(
            files=["src/auth_session.py"],
            added={"src/auth_session.py": ["def login(): pass"]},
            loc=1,
        )
        assert result["tier"] == "Full"
        assert "auth" in result["sensitive_patterns"]
        assert "session" in result["sensitive_patterns"]

    def test_sensitive_path_in_subdir(self) -> None:
        result = vt.recommend_tier(
            files=["server/payment_processor.py"],
            added={"server/payment_processor.py": ["x = 1"]},
            loc=1,
        )
        assert result["tier"] == "Full"
        assert "payment" in result["sensitive_patterns"]

    def test_sensitive_auth_does_not_overmatch_authority(self) -> None:
        # `authority.py` should NOT trigger `auth` sensitive pattern.
        result = vt.recommend_tier(
            files=["src/authority.py"],
            added={"src/authority.py": ["class Authority: pass"]},
            loc=1,
        )
        assert result["tier"] == "Light"
        assert not result.get("sensitive_patterns")

    def test_sensitive_session_does_not_overmatch_sessionizer(self) -> None:
        # `sessionizer.py` should NOT trigger `session` sensitive pattern.
        result = vt.recommend_tier(
            files=["src/sessionizer.py"],
            added={"src/sessionizer.py": ["class Sessionizer: pass"]},
            loc=1,
        )
        assert result["tier"] == "Light"
        assert not result.get("sensitive_patterns")

    def test_sensitive_auth_session_still_matches(self) -> None:
        # The Wave 3 regression: `auth_session.py` MUST still trigger.
        result = vt.recommend_tier(
            files=["src/auth_session.py"],
            added={"src/auth_session.py": ["def login(): pass"]},
            loc=1,
        )
        assert result["tier"] == "Full"
        assert (
            "auth" in result["sensitive_patterns"]
            or "session" in result["sensitive_patterns"]
        )

    def test_small_change_is_light(self) -> None:
        result = vt.recommend_tier(
            files=["util.py"],
            added={"util.py": ["def add(a, b):", "    return a + b"]},
            loc=2,
        )
        assert result["tier"] == "Light"

    def test_large_change_is_full(self) -> None:
        added = {"util.py": [f"line{i}" for i in range(50)]}
        result = vt.recommend_tier(files=["util.py"], added=added, loc=50)
        assert result["tier"] == "Full"
        assert "≥" in result["reason"]

    def test_threshold_boundary_29_is_light(self) -> None:
        added = {"util.py": [f"l{i}" for i in range(29)]}
        result = vt.recommend_tier(files=["util.py"], added=added, loc=29)
        assert result["tier"] == "Light"

    def test_threshold_boundary_30_is_full(self) -> None:
        added = {"util.py": [f"l{i}" for i in range(30)]}
        result = vt.recommend_tier(files=["util.py"], added=added, loc=30)
        assert result["tier"] == "Full"

    def test_marker_block_supersedes_sensitive(self) -> None:
        # If both forbidden markers AND sensitive paths are present, BLOCK wins.
        result = vt.recommend_tier(
            files=["src/auth.py"],
            added={"src/auth.py": ["def f(): # FIXME bug"]},
            loc=1,
        )
        assert result["tier"] == "BLOCK"

    def test_env_example_alone_is_skip(self) -> None:
        # Template env files are placeholders — alone they should be Skip.
        result = vt.recommend_tier(
            files=[".env.example"],
            added={".env.example": ["DB_URL=<url>"]},
            loc=1,
        )
        assert result["tier"] == "Skip"

    def test_env_supabase_example_alone_is_skip(self) -> None:
        # Multi-dot template variants must also be recognized as skippable.
        # Regression: `.env.supabase.example` used to escalate to Full via the
        # `.env` sensitive pattern, blocking a docs-only commit.
        result = vt.recommend_tier(
            files=[".env.supabase.example"],
            added={".env.supabase.example": ["DB_URL=postgres://<creds>@..."]},
            loc=1,
        )
        assert result["tier"] == "Skip"

    def test_env_template_variants_are_skip(self) -> None:
        # Every suffix in the module's template list must be treated as Skip.
        # Reads the constant directly so adding a new suffix auto-extends coverage.
        for suffix in vt._TEMPLATE_SUFFIXES:
            fname = f".env.{suffix}"
            result = vt.recommend_tier(
                files=[fname],
                added={fname: ["X=y"]},
                loc=1,
            )
            assert result["tier"] == "Skip", f"{fname} should be Skip"

    def test_env_example_mixed_with_code_does_not_escalate(self) -> None:
        # Mixed diff: env template + regular code. The template must not
        # contribute a `.env` sensitive hit. Tier falls to LOC-based Light.
        # Covers both single-dot (.env.example) and multi-dot (.supabase) forms
        # so mutations that narrow the regex to either shape are caught.
        for template in (".env.example", ".env.supabase.example"):
            result = vt.recommend_tier(
                files=[template, "src/util.py"],
                added={
                    template: ["DB_URL=<url>"],
                    "src/util.py": ["def add(a, b):", "    return a + b"],
                },
                loc=3,
            )
            assert result["tier"] == "Light", f"{template} should not escalate"
            assert ".env" not in result["sensitive_patterns"], (
                f"{template} should not contribute .env sensitive hit"
            )

    def test_env_real_file_still_sensitive(self) -> None:
        # Real .env MUST still escalate — this is what the .env pattern is for.
        result = vt.recommend_tier(
            files=[".env", "src/util.py"],
            added={".env": ["SECRET=x"], "src/util.py": ["pass"]},
            loc=2,
        )
        assert result["tier"] == "Full"
        assert ".env" in result["sensitive_patterns"]

    def test_env_supabase_real_still_sensitive(self) -> None:
        # Real .env.supabase (no .example suffix) MUST still escalate.
        result = vt.recommend_tier(
            files=[".env.supabase", "src/util.py"],
            added={".env.supabase": ["PG=x"], "src/util.py": ["pass"]},
            loc=2,
        )
        assert result["tier"] == "Full"
        assert ".env" in result["sensitive_patterns"]

    def test_env_template_in_subdirectory_is_skip(self) -> None:
        # Path anchor `(^|/)` must treat subdirectory templates identically.
        result = vt.recommend_tier(
            files=["config/.env.supabase.example"],
            added={"config/.env.supabase.example": ["DB=x"]},
            loc=1,
        )
        assert result["tier"] == "Skip"

    def test_env_template_case_insensitive(self) -> None:
        # IGNORECASE flag must be preserved — dropping it would narrow coverage.
        result = vt.recommend_tier(
            files=[".ENV.EXAMPLE", "src/util.py"],
            added={".ENV.EXAMPLE": ["DB=x"], "src/util.py": ["pass"]},
            loc=2,
        )
        assert ".env" not in result["sensitive_patterns"]

    def test_env_example_with_trailing_extension_still_sensitive(self) -> None:
        # `.env.example.bak` is a backup of a template — may hold real values.
        # The `$` anchor on the negative lookahead must reject trailing junk.
        result = vt.recommend_tier(
            files=[".env.example.bak"],
            added={".env.example.bak": ["SECRET=x"]},
            loc=1,
        )
        assert result["tier"] == "Full"
        assert ".env" in result["sensitive_patterns"]

    def test_env_local_and_production_still_sensitive(self) -> None:
        # Common real secret-bearing variants that must NOT be exempted.
        for name in (".env.local", ".env.production", ".envrc", ".env.prod.bak"):
            result = vt.recommend_tier(
                files=[name, "src/util.py"],
                added={name: ["SECRET=x"], "src/util.py": ["pass"]},
                loc=2,
            )
            assert result["tier"] == "Full", f"{name} should still escalate"
            assert ".env" in result["sensitive_patterns"], (
                f"{name} should match .env sensitive pattern"
            )

    def test_harness_session_state_with_code_does_not_escalate(self) -> None:
        result = vt.recommend_tier(
            files=[".claude/session/state.json", "src/util.py"],
            added={
                ".claude/session/state.json": ['{"user_message_count": 12}'],
                "src/util.py": ["def add(a, b):", "    return a + b"],
            },
            loc=3,
        )
        assert result["tier"] == "Light"
        assert "session" not in result["sensitive_patterns"]

    def test_harness_session_dir_other_files_exempt(self) -> None:
        result = vt.recommend_tier(
            files=[".claude/session/feedback_captured.json", "src/util.py"],
            added={
                ".claude/session/feedback_captured.json": ['[{"saved_at": "x"}]'],
                "src/util.py": ["pass"],
            },
            loc=2,
        )
        assert "session" not in result["sensitive_patterns"]

    def test_real_session_code_path_still_escalates(self) -> None:
        result = vt.recommend_tier(
            files=["src/auth/session.py"],
            added={"src/auth/session.py": ["def login(): pass"]},
            loc=1,
        )
        assert result["tier"] == "Full"
        assert "session" in result["sensitive_patterns"]

    def test_top_level_session_module_still_escalates(self) -> None:
        result = vt.recommend_tier(
            files=["src/session.py"],
            added={"src/session.py": ["def login(): pass"]},
            loc=1,
        )
        assert result["tier"] == "Full"
        assert "session" in result["sensitive_patterns"]

    def test_other_skill_session_logs_not_exempt(self) -> None:
        result = vt.recommend_tier(
            files=[
                "skills/some-helper/session-log.json",
                "src/util.py",
            ],
            added={
                "skills/some-helper/session-log.json": ['{"x": 1}'],
                "src/util.py": ["pass"],
            },
            loc=2,
        )
        assert result["tier"] == "Full"
        assert "session" in result["sensitive_patterns"]

    def test_schedule_session_file_does_not_escalate(self) -> None:
        result = vt.recommend_tier(
            files=["public/js/schedule-session.js"],
            added={"public/js/schedule-session.js": ["function open() {}"]},
            loc=1,
        )
        assert result["tier"] == "Light"
        assert "session" not in result["sensitive_patterns"]

    def test_booking_session_file_does_not_escalate(self) -> None:
        result = vt.recommend_tier(
            files=["src/booking-session.ts"],
            added={"src/booking-session.ts": ["export const x = 1;"]},
            loc=1,
        )
        assert result["tier"] == "Light"
        assert "session" not in result["sensitive_patterns"]

    def test_auth_session_still_escalates_near_miss(self) -> None:
        # near-miss: file name CONTAINS "session" but is NOT one of the calendar
        # prefixes — should still escalate
        result = vt.recommend_tier(
            files=["src/user-session.py"],
            added={"src/user-session.py": ["def login(): pass"]},
            loc=1,
        )
        assert result["tier"] == "Full"
        assert "session" in result["sensitive_patterns"]

    def test_session_subdirectory_still_escalates(self) -> None:
        result = vt.recommend_tier(
            files=[".claude/session/sub/state.json", "src/util.py"],
            added={
                ".claude/session/sub/state.json": ['{"x": 1}'],
                "src/util.py": ["pass"],
            },
            loc=2,
        )
        assert result["tier"] == "Full"
        assert "session" in result["sensitive_patterns"]

    def test_session_non_json_still_escalates(self) -> None:
        result = vt.recommend_tier(
            files=[".claude/session/auth_handler.py"],
            added={".claude/session/auth_handler.py": ["def login(): pass"]},
            loc=1,
        )
        assert result["tier"] == "Full"
        assert "session" in result["sensitive_patterns"]

    @pytest.mark.parametrize(
        "path",
        [
            "hooks/session_start.py",
            "tests/test_session_start.py",
            "tests/test_session_start_hook_drift.py",
            "tests/test_session_start_budget.py",
            ".claude/hooks/session_start.py",
            ".claude/hooks/tests/test_session_start.py",
        ],
    )
    def test_harness_session_start_hook_does_not_escalate(self, path: str) -> None:
        # The SessionStart lifecycle hook is not auth code,
        # but `_` is a segment boundary so `session` matched its filename.
        result = vt.recommend_tier(files=[path], added={path: ["x = 1"]}, loc=1)
        assert result["tier"] == "Light"
        assert "session" not in result["sensitive_patterns"]

    @pytest.mark.parametrize(
        "path",
        [
            "src/session_start.py",
            "hooks/session_token.py",
            "hooks/sub/session_start.py",
        ],
    )
    def test_session_start_near_miss_still_escalates(self, path: str) -> None:
        result = vt.recommend_tier(files=[path], added={path: ["x = 1"]}, loc=1)
        assert result["tier"] == "Full"
        assert "session" in result["sensitive_patterns"]

    @pytest.mark.parametrize(
        ("path", "pattern"),
        [
            ("hooks/session_start_token.py", "token"),
            ("tests/test_session_start_auth.py", "auth"),
            ("tests/test_session_start_secret_drift.py", "secret"),
            # pins the trailing `$`
            ("hooks/session_start.py_token.py", "token"),
            # pins the leading `(^|/)` boundary
            ("x.claude/hooks/session_start.py", "session"),
        ],
    )
    def test_session_start_suffix_does_not_smuggle_other_patterns(
        self, path: str, pattern: str
    ) -> None:
        # An exempt path skips every sensitive pattern, so the exemption must
        # name exact files rather than accept any suffix.
        result = vt.recommend_tier(files=[path], added={path: ["x = 1"]}, loc=1)
        assert result["tier"] == "Full"
        assert pattern in result["sensitive_patterns"]

    @pytest.mark.parametrize(
        ("added", "deleted"),
        [
            (["    argv_dsn, env = psql_env(conn)"], []),
            (['    subprocess.run(["psql", conn])'], []),
            ([], ["    argv_dsn, env = psql_env(conn)"]),
            (['    if Path(".env.supabase").exists():'], []),
            (['    conn = os.environ.get("X")'], []),
            ([], ["            env=env,"]),
            (["from common import resolve_connection_string"], []),
        ],
    )
    def test_session_start_credential_edit_still_escalates(
        self, added: list[str], deleted: list[str]
    ) -> None:
        # The hook holds the psql_env DSN split; editing that plumbing must keep
        # its Full gate even though the filename is exempt.
        path = "hooks/session_start.py"
        result = vt.recommend_tier(
            files=[path], added={path: added}, loc=1, deleted={path: deleted}
        )
        assert result["tier"] == "Full"
        assert result["sensitive_patterns"] == ["credential-content"]

    def test_content_guard_is_scoped_to_session_start(self) -> None:
        # Other exemptions keep their old behaviour: content is not inspected.
        path = "public/js/schedule-session.js"
        result = vt.recommend_tier(
            files=[path], added={path: ["const token = getSlot();"]}, loc=1
        )
        assert result["tier"] == "Light"

    def test_exempt_file_alone_is_skip(self) -> None:
        result = vt.recommend_tier(
            files=[".claude/session/state.json"],
            added={".claude/session/state.json": ['{"x": 1}']},
            loc=1,
        )
        assert result["tier"] == "Skip"


def _tier(*paths: str) -> dict:
    """recommend_tier over `paths`, each with one changed line."""
    return vt.recommend_tier(
        files=list(paths), added={p: ["k: v"] for p in paths}, loc=len(paths)
    )


class TestConfigSensitiveBeforeSkip:
    """A config-only diff goes through the sensitive-path check; prose does not.

    Previously the config/docs Skip ran FIRST, so
    `config/secrets.yaml` alone was Skipped — secrets in config were left to
    gitleaks, which catches secret VALUES, not a risky change to secret wiring.
    """

    @pytest.mark.parametrize(
        ("paths", "pattern"),
        [
            (("config/secrets.yaml",), "secret"),
            (("deploy/auth.json",), "auth"),
            (("infra/token.toml",), "token"),
            (("db/schema.yml",), "schema"),
            (("app/permissions.ini",), "permission"),
            (("credentials.cfg",), "credential"),
            (("README.md", "config/secrets.yaml"), "secret"),
            # "prose" is an extension, not a directory: a docs/ artifact is checked
            (("docs/token-rotation.txt",), "token"),
            # the sensitive check outranks the build-tooling Light cap
            (("Taskfile.yml", "config/secrets.yaml"), "secret"),
        ],
    )
    def test_escalates_to_full(self, paths: tuple[str, ...], pattern: str) -> None:
        result = _tier(*paths)
        assert result["tier"] == "Full"
        assert pattern in result["sensitive_patterns"]

    @pytest.mark.parametrize(
        "paths",
        [
            ("docs/security.md",),
            ("docs/auth-flow.rst",),
            ("secret-rotation.adoc",),
            ("SECURITY.MD",),
            ("config/app.yaml",),
            # a doc's NAME is not a reason to review a config diff with six agents
            ("docs/security.md", "config/app.yaml"),
        ],
    )
    def test_stays_skip(self, paths: tuple[str, ...]) -> None:
        assert _tier(*paths)["tier"] == "Skip"

    def test_comment_only_edit_to_sensitive_config_is_full(self) -> None:
        # Zero effective LOC must not Skip a sensitive path either.
        result = vt.recommend_tier(
            files=["config/secrets.yaml"], added={"config/secrets.yaml": ["# note"]}, loc=0
        )
        assert result["tier"] == "Full"

    @pytest.mark.parametrize(
        ("paths", "tier"),
        [
            # a prose NAME never escalates, whatever it sits next to
            (("docs/security.md", "src/util.py"), "Light"),
            (("SECURITY.md", "Makefile"), "Light"),
            # while the code file's own name still does
            (("docs/guide.md", "src/auth_session.py"), "Full"),
        ],
    )
    def test_prose_name_is_not_a_signal_beside_code(
        self, paths: tuple[str, ...], tier: str
    ) -> None:
        result = _tier(*paths)
        assert result["tier"] == tier
        assert "security" not in result["sensitive_patterns"]

    @pytest.mark.parametrize("path", ["guide.rst", "book/ch1.adoc", "NOTES.MD"])
    def test_prose_is_exempt_from_the_marker_scan(self, path: str) -> None:
        # Consistent with .md: prose mentions TODO as a word, not as a marker.
        assert vt.find_forbidden_markers({path: ["# TODO: fix the docs"]}) == []


class TestFindForbiddenMarkers:
    def test_skips_markdown_files(self) -> None:
        added = {"docs.md": ["TODO: write docs", "FIXME: typo"]}
        assert vt.find_forbidden_markers(added) == []

    def test_skips_json_yaml_configs(self) -> None:
        added = {
            "config.json": ['"label": "TODO"'],
            "stack.yaml": ["note: HACK in deploy"],
        }
        assert vt.find_forbidden_markers(added) == []

    def test_finds_in_python(self) -> None:
        added = {"src/foo.py": ["    pass  # TODO later"]}
        hits = vt.find_forbidden_markers(added)
        assert len(hits) == 1
        assert "TODO" in hits[0]
        assert "src/foo.py" in hits[0]

    def test_scanner_source_is_exempt(self) -> None:
        # validate_tier.py defines FORBIDDEN_MARKERS = ["TODO", ...] — those
        # literal strings would otherwise block on every edit to the scanner.
        added = {
            "hooks/validate_tier.py": [
                'FORBIDDEN_MARKERS = ["TODO", "FIXME", "HACK"]',
            ],
        }
        assert vt.find_forbidden_markers(added) == []

    def test_scanner_test_file_is_exempt(self) -> None:
        # The scanner's test file uses marker strings as fixture input.
        added = {
            "tests/test_validate_tier.py": [
                'added = {"src/foo.py": ["    pass  # TODO"]}',
            ],
        }
        assert vt.find_forbidden_markers(added) == []

    def test_word_boundary_avoids_substring_false_positive(self) -> None:
        # 'todoist' should NOT match TODO (word-bound regex).
        added = {"src/foo.py": ["from todoist import api"]}
        assert vt.find_forbidden_markers(added) == []


class TestMarkerNeedsCommentPrefixOrColon:
    """A marker must look like a note-to-self, not merely contain the word.

    Three of the six FORBIDDEN_MARKERS are ordinary words or common
    identifiers — `TEMP`, `FIX`, `XXX` — so a bare `\\bTEMP\\b` match blocked
    legitimate CODE, not just prose. The skip-pattern exemptions above don't
    help: those only cover .md/.json/.yaml, and the real incident
    was a Python test assertion on display labels.

    This matters more than a lint nit because find_forbidden_markers drives
    exit code 2 / BLOCK, which halts the gate before any validation runs. A
    gate that blocks on ("CPU", "41%", "TEMP", "57C") is a gate that gets
    worked around, and a bypassed gate protects nothing.
    """

    def test_bare_marker_word_in_code_does_not_block(self) -> None:
        """The actual incident, as a regression test."""
        added = {
            "tests/test_oled.py": ['    assert cells == ("CPU", "41%", "TEMP", "57C")'],
        }
        assert vt.find_forbidden_markers(added) == []

    def test_marker_as_identifier_does_not_block(self) -> None:
        added = {"src/paths.py": ['TEMP_DIR = Path("/tmp")', "FIX_VERSION = 3"]}
        assert vt.find_forbidden_markers(added) == []

    def test_comment_prefixed_marker_still_blocks(self) -> None:
        """The narrowing must not cost a single true positive."""
        for line in ("    # TODO refactor", "// FIXME broken", "  #HACK no space"):
            added = {"src/foo.py": [line]}
            assert vt.find_forbidden_markers(added), f"missed a real marker: {line}"

    def test_colon_suffixed_marker_still_blocks(self) -> None:
        """`TODO:` with no comment prefix — e.g. inside a docstring."""
        added = {"src/foo.py": ['    """TODO: document this."""']}
        assert vt.find_forbidden_markers(added)

    def test_every_marker_is_still_caught_in_its_normal_form(self) -> None:
        """Enumerate FORBIDDEN_MARKERS rather than listing them.

        A hardcoded list here would silently stop covering a marker the moment
        someone adds one — the same defect this repo's psql guard exists to
        avoid.
        """
        for marker in vt.FORBIDDEN_MARKERS:
            added = {"src/foo.py": [f"    # {marker} something"]}
            assert vt.find_forbidden_markers(added), f"{marker} no longer blocks"


class TestAllFilesSkippable:
    def test_empty_is_skip(self) -> None:
        assert vt.all_files_skippable([]) is True

    def test_all_md_is_skip(self) -> None:
        assert vt.all_files_skippable(["a.md", "b.md", "docs/c.md"]) is True

    def test_one_python_breaks_skip(self) -> None:
        assert vt.all_files_skippable(["a.md", "b.py"]) is False


# ---------- end-to-end tests (real git repo) ----------


@pytest.fixture
def git_repo(tmp_path: Path):
    """Make a real git repo in a tmp directory and yield (path, run_diff_fn)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cwd = os.getcwd()
    os.chdir(repo)
    try:
        subprocess.run(["git", "init", "-q"], check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "config", "user.name", "t"], check=True)
        subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", "init"], check=True
        )

        def run(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                [sys.executable, str(SCRIPT), *args],
                capture_output=True,
                text=True,
            )

        yield repo, run
    finally:
        os.chdir(cwd)


class TestEndToEnd:
    def test_empty_diff(self, git_repo) -> None:
        _, run = git_repo
        proc = run("HEAD")
        assert proc.returncode == 0
        assert "Skip" in proc.stdout

    def test_marker_in_code_blocks(self, git_repo) -> None:
        repo, run = git_repo
        (repo / "bar.py").write_text("def foo():\n    pass  # TODO fix\n")
        subprocess.run(["git", "add", "bar.py"], cwd=repo, check=True)
        proc = run("--staged")
        assert proc.returncode == 2
        assert "BLOCK" in proc.stdout
        assert "TODO" in proc.stdout

    def test_marker_in_markdown_does_not_block(self, git_repo) -> None:
        repo, run = git_repo
        (repo / "notes.md").write_text("Watch for TODO markers in code.\n")
        subprocess.run(["git", "add", "notes.md"], cwd=repo, check=True)
        proc = run("--staged")
        assert proc.returncode == 0
        assert "Skip" in proc.stdout

    def test_sensitive_path_full_tier(self, git_repo) -> None:
        repo, run = git_repo
        (repo / "src").mkdir()
        (repo / "src" / "auth_session.py").write_text("def login(): pass\n")
        subprocess.run(["git", "add", "src/auth_session.py"], cwd=repo, check=True)
        proc = run("--staged")
        assert proc.returncode == 0
        assert "Full" in proc.stdout
        assert "auth" in proc.stdout

    def test_sensitive_config_only_diff_is_full(self, git_repo) -> None:
        repo, run = git_repo
        (repo / "config").mkdir()
        (repo / "config" / "secrets.yaml").write_text("db: x\n")
        subprocess.run(["git", "add", "config"], cwd=repo, check=True)
        data = json.loads(run("--staged", "--json").stdout)
        assert data["tier"] == "Full"
        assert "secret" in data["sensitive_patterns"]

    def test_renaming_sensitive_config_away_is_full(self, git_repo) -> None:
        repo, run = git_repo
        (repo / "config").mkdir()
        (repo / "config" / "secrets.yaml").write_text("db: x\nport: 5432\n")
        subprocess.run(["git", "add", "config"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "c"], cwd=repo, check=True)
        subprocess.run(["git", "mv", "config/secrets.yaml", "config/app.yaml"], cwd=repo, check=True)
        data = json.loads(run("--staged", "--json").stdout)
        assert data["tier"] == "Full"
        assert "secret" in data["sensitive_patterns"]

    def test_json_output(self, git_repo) -> None:
        repo, run = git_repo
        (repo / "util.py").write_text("def add(a, b):\n    return a + b\n")
        subprocess.run(["git", "add", "util.py"], cwd=repo, check=True)
        proc = run("--staged", "--json")
        assert proc.returncode == 0
        data = json.loads(proc.stdout)
        assert data["tier"] == "Light"
        assert data["loc"] == 2
        assert data["files"] == 1
