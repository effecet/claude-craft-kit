"""validate_tier.py stops counting prose, comments and build files as code.

One defect, three shapes: the tier is picked from a LOC count that includes
lines no reviewer agent needs to read.
  - Comment lines counted at full weight (a comment-only .py change plus an
    8-line workflow scored 64 LOC and went Full).
  - Prose counted as LOC (1,400 lines of Markdown + 50 of code → Full).
  - A 27-line Makefile fix went Full.

The invariants that must NOT move: deleted code keeps full weight, one real
line among hundreds of comments still counts, sensitive paths escalate whatever
the extension (build files included), build files are never Skip, and forbidden
markers are still scanned in build files. The gate review also found the diff
parser trusting `---`/`+++` anywhere — a code line `++ b/notes.md` made every
later line "prose" and routed 40 lines of exec() to Skip — and dropping renames
and binary changes from the file list entirely.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

import validate_tier as vt  # noqa: E402


# ── comment / blank discount ──────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("path", "comment"),
    [
        ("src/a.py", "# why this exists"),
        ("scripts/x.sh", "   # indented"),
        ("src/a.ts", "// note"),
        ("src/a.ts", "/* one-line block */"),
        ("db/q.sql", "-- explain"),
        ("Makefile", "# target docs"),
    ],
)
def test_comment_only_added_lines_do_not_count(path: str, comment: str) -> None:
    assert vt.effective_loc({path: [comment, "", "   "]}, 3) == 0


def test_a_multi_line_block_comment_is_tracked_across_lines() -> None:
    doc = ["/**", " * Explains the function.", " *", " * @param x the input", " */"]
    assert vt.effective_loc({"src/a.ts": doc}, 5) == 0


@pytest.mark.parametrize(
    "code",
    [
        "* evil()",  # multiplication continuation / C pointer write outside a block
        "*p = x;",
        "*[Symbol.iterator]() {",  # generator method
        "/* ok */ evil()",  # code after a closed comment
        "* { box-sizing: border-box; }",  # CSS universal selector
    ],
)
def test_star_prefixed_code_is_not_a_comment(code: str) -> None:
    path = "src/a.css" if code.startswith("* {") else "src/a.ts"
    assert vt.effective_loc({path: [code]}, 1) == 1


def test_code_after_a_block_close_is_code() -> None:
    assert vt.effective_loc({"src/a.ts": ["/* start", "end */ evil();"]}, 2) == 1


def test_one_real_line_among_many_comments_still_counts() -> None:
    added = {"src/a.py": ["# c"] * 200 + ["x = 1"]}
    assert vt.effective_loc(added, 201) == 1


def test_trailing_comments_and_urls_are_code() -> None:
    added = {
        "src/a.py": ["x = 1  # trailing comment is still code", "url = 'http://x'"],
        "src/a.ts": ["const u = 'http://x'; // trailing", "a = b / c;"],
        "db/q.sql": ["SELECT 1; -- trailing"],
    }
    assert vt.effective_loc(added, 5) == 5


def test_hash_is_not_a_comment_in_a_language_that_does_not_use_it() -> None:
    assert vt.effective_loc({"src/a.ts": ["  #count = 0;"]}, 1) == 1  # JS private field


def test_a_shebang_is_code() -> None:
    assert vt.effective_loc({"scripts/x.sh": ["#!/usr/bin/env bash"]}, 1) == 1


def test_unknown_file_types_never_discount() -> None:
    assert vt.effective_loc({"data/x.csv": ["# not a comment here"]}, 1) == 1


# ── deletions ─────────────────────────────────────────────────────────────────
def test_deleted_code_keeps_full_weight() -> None:
    assert vt.effective_loc({"src/a.py": ["# gone"]}, 41, deleted={"src/a.py": ["x = 1"] * 40}) == 40


def test_deleted_comments_are_discounted_like_added_ones() -> None:
    # Rewording 60 comment lines is neither Full when deleted nor Skip when added.
    assert vt.effective_loc({"src/a.py": ["# new"] * 60}, 120, deleted={"src/a.py": ["# old"] * 60}) == 0


# ── prose ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("path", ["README.md", "docs/guide.rst", "book/ch1.adoc"])
def test_prose_lines_do_not_count_added_or_deleted(path: str) -> None:
    added = {path: ["words"] * 1400, "src/a.py": ["x = 1"] * 50}
    assert vt.effective_loc(added, 1400 + 50 + 300, deleted={path: ["old"] * 300}) == 50


@pytest.mark.parametrize("path", ["requirements.txt", "CMakeLists.txt", "notes.txt"])
def test_txt_is_not_prose(path: str) -> None:
    # requirements.txt pins and CMakeLists.txt are real changes.
    assert vt.effective_loc({path: ["numpy==2.1.0"] * 5}, 5) == 5


def test_config_lines_still_count() -> None:
    added = {".forgejo/workflows/ci.yml": ["run: make test"] * 40, "src/a.py": ["x = 1"]}
    assert vt.effective_loc(added, 41) == 41


def test_breakdown_reports_each_discount() -> None:
    added = {"src/a.py": ["# c", "x = 1"], "README.md": ["prose", "more"]}
    b = vt.loc_breakdown(added, 5, deleted={"README.md": ["x"]})
    assert b == {"raw": 5, "migration_copies": 0, "comments_blank": 1, "prose": 3, "lockfile": 0, "effective": 1}


def test_a_copied_migration_comment_counts_once_as_a_copy(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "drizzle").mkdir()
    (tmp_path / "drizzle" / "0013_old.sql").write_text("-- shared header\nSELECT 1;\n")
    monkeypatch.chdir(tmp_path)
    added = {"drizzle/0014_new.sql": ["-- shared header", "SELECT 1;", "-- new note", "SELECT 2;"]}
    b = vt.loc_breakdown(added, 4)
    assert b["migration_copies"] == 2 and b["comments_blank"] == 1 and b["effective"] == 1


# ── build files ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("path", ["Makefile", "sub/Makefile", "GNUmakefile", "justfile", "rules.mk", "Taskfile.yml"])
def test_build_file_only_diff_is_capped_at_light(path: str) -> None:
    res = vt.recommend_tier([path], {path: ["\t@echo x"] * 200}, 200)
    assert res["tier"] == "Light"


def test_build_file_plus_docs_is_still_light_not_skip() -> None:
    res = vt.recommend_tier(["Makefile", "README.md"], {"Makefile": ["\tx"], "README.md": ["y"]}, 1)
    assert res["tier"] == "Light"


@pytest.mark.parametrize("path", ["auth/Makefile", "migrations/Makefile", "secrets.mk", "security/Makefile"])
def test_a_sensitive_build_file_still_escalates(path: str) -> None:
    res = vt.recommend_tier([path], {path: ["\tpsql $$DATABASE_URL"] * 40}, 40)
    assert res["tier"] == "Full"


@pytest.mark.parametrize("path", ["Makefile.generator.py", "src/makefile_utils.py", "pyproject.toml.py"])
def test_build_patterns_are_anchored_to_the_whole_name(path: str) -> None:
    assert not any(re.search(p, path) for p in vt.BUILD_PATTERNS)


def test_pyproject_is_not_a_build_file() -> None:
    assert not any(re.search(p, "pyproject.toml") for p in vt.BUILD_PATTERNS)


def test_build_file_plus_code_still_judged_on_the_code() -> None:
    files = ["Makefile", "src/a.py"]
    added = {"Makefile": ["\t@echo x"] * 10, "src/a.py": ["x = 1"] * 40}
    assert vt.recommend_tier(files, added, 50)["tier"] == "Full"


def test_markers_are_still_scanned_in_build_files() -> None:
    res = vt.recommend_tier(["Makefile"], {"Makefile": ["# TODO: remove this hack"]}, 1)
    assert res["tier"] == "BLOCK"


# ── marker-scan exemption stays narrow ────────────────────────────────────────
@pytest.mark.parametrize(
    ("path", "exempt"),
    [
        ("tests/test_validate_tier_loc_discounts.py", True),
        ("tests/test_validate_tier.py", True),
        ("src/test_validate_tier_evil.py", False),
        ("tests/test_validate_tier_x.pyx", False),
    ],
)
def test_marker_exemption_is_anchored_to_the_hook_tests(path: str, exempt: bool) -> None:
    assert any(re.search(p, path) for p in vt.MARKER_SCAN_EXEMPT) is exempt


# ── tier consequences ─────────────────────────────────────────────────────────
def test_comment_only_code_change_lands_on_skip() -> None:
    res = vt.recommend_tier(["src/a.py"], {"src/a.py": ["# clarify"] * 60}, 0)
    assert res["tier"] == "Skip"


def test_sensitive_path_still_escalates_even_at_zero_effective_loc() -> None:
    res = vt.recommend_tier(["src/auth.py"], {"src/auth.py": ["# note"]}, 0)
    assert res["tier"] == "Full"


def test_print_human_shows_the_breakdown(capsys) -> None:
    vt.print_human({
        "tier": "Light", "reason": "r", "files": 2, "loc": 3,
        "loc_breakdown": {"raw": 20, "migration_copies": 4, "comments_blank": 7, "prose": 6, "effective": 3},
    })
    out = capsys.readouterr().out
    assert "20 raw" in out and "7 comment/blank" in out and "6 prose" in out
    assert "4 copied from a sibling migration" in out


# ── end to end through git ────────────────────────────────────────────────────
SCRIPT = Path(__file__).resolve().parent.parent / "hooks" / "validate_tier.py"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "q.sql").write_text("-- c1\n-- c2\nSELECT 1;\n")
    (tmp_path / "README.md").write_text("old\n" * 100)
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def _tier(repo: Path) -> dict:
    _git(repo, "add", "-A")
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--staged", "--json"], cwd=repo, capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def test_e2e_docs_heavy_code_light_diff_is_light(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "README.md").write_text("new prose line\n" * 400)  # 100 deleted + 400 added
    (repo / "a.py").write_text("x = 1\n" + "# why\n" * 50 + "y = 2\n")
    res = _tier(repo)
    assert res["tier"] == "Light", res
    assert res["loc"] == 1 and res["loc_breakdown"]["prose"] == 500


def test_e2e_deleted_code_file_is_counted(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "big.py").write_text("v = 0\n" * 60)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "big")
    (repo / "big.py").unlink()
    res = _tier(repo)
    assert res["loc"] == 60 and res["tier"] == "Full", res


def test_e2e_deleted_sql_comment_lines_are_not_read_as_headers(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "q.sql").write_text("SELECT 1;\n")  # deletes `-- c1`, `-- c2`, shown as `--- c1`
    res = _tier(repo)
    assert res["raw_loc"] == 2 and res["loc_breakdown"]["comments_blank"] == 2, res


def test_e2e_a_fake_header_inside_a_hunk_cannot_launder_code_as_prose(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "app.ts").write_text("++ b/notes.md\n" + "exec(payload);\n" * 40)
    res = _tier(repo)
    assert res["tier"] == "Full" and res["loc_breakdown"]["prose"] == 0, res


def test_e2e_a_rename_into_a_sensitive_path_escalates(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "mv", "a.py", "auth.py")  # 100% similarity: no ---/+++ lines at all
    res = _tier(repo)
    assert res["tier"] == "Full" and "auth" in res["sensitive_patterns"], res


def test_e2e_a_binary_change_is_listed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "logo.bin").write_bytes(bytes(range(256)))
    res = _tier(repo)
    assert res["files"] == 1, res


def test_e2e_a_users_git_diff_config_cannot_blank_the_file_list(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "config", "diff.noprefix", "true")  # would rename every a/ b/ header
    (repo / "a.py").write_text("x = 1\n" + "y = 2\n" * 40)
    res = _tier(repo)
    assert res["tier"] == "Full" and res["loc"] == 40, res


# ── round-3 gate findings ─────────────────────────────────────────────────────
def test_an_unclosed_block_does_not_mask_the_rest_of_the_file() -> None:
    # Editing the first line of an existing doc comment: its `*/` is in context.
    added = ["/** Updated header"] + [f"evil{i}();" for i in range(40)]
    assert vt.effective_loc({"src/a.js": added}, 41) >= 40


def test_markers_are_scanned_in_a_yml_build_file() -> None:
    res = vt.recommend_tier(["Taskfile.yml"], {"Taskfile.yml": ["# TODO: x"]}, 1)
    assert res["tier"] == "BLOCK"


@pytest.mark.parametrize(
    ("line", "want"),
    [
        ('+++ "b/we\\"ird.py"', 'we"ird.py'),
        ('+++ "b/tab\\there.py"', "tab\there.py"),
        ("+++ b/sp ace.py\t", "sp ace.py"),
        ("+++ /dev/null", None),
    ],
)
def test_header_paths_are_unquoted(line: str, want: str | None) -> None:
    assert vt._header_path(line, "b/") == want


def test_e2e_a_quoted_path_still_counts_its_lines(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / 'we"ird.py').write_text("exec(payload)\n" * 41)
    res = _tier(repo)
    assert res["tier"] == "Full" and res["loc"] == 41, res


def test_a_hunk_with_no_nameable_file_fails_closed(monkeypatch) -> None:
    patch = "diff --git a/x b/x\n@@ -0,0 +1,2 @@\n+evil()\n+evil()\n"
    monkeypatch.setattr(vt, "_git_diff", lambda extra: "" if "--name-status" in extra else patch)
    files, added, loc, _ = vt.get_diff("HEAD")
    assert loc == 2 and added[vt.UNKNOWN_PATH] == ["evil()", "evil()"]
    assert vt.UNKNOWN_PATH in files and not vt.all_files_skippable(files)


def test_e2e_two_edited_doc_comments_do_not_pair_up_across_context(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    body = "/**\n * A\n */\nfunction a() {}\n" + "keep();\n" * 5 + "/**\n * B\n */\nfunction b() {}\n"
    (repo / "m.js").write_text(body)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "js")
    # Edit A's opening line and B's closing line; add real code in between.
    new = body.replace("/**\n * A", "/** A new header\n * A", 1).replace(" * B\n */", " * B\n   end of B */", 1)
    # Insert (no deletions) so only unchanged context separates the runs.
    new = new.replace("keep();\n" * 5, "keep();\n" * 3 + "evil();\n" * 40 + "keep();\n" * 2, 1)
    (repo / "m.js").write_text(new)
    res = _tier(repo)
    assert res["loc"] >= 40 and res["tier"] == "Full", res


def test_unchanged_context_lines_end_a_run(monkeypatch) -> None:
    patch = (
        "diff --git a/m.js b/m.js\n--- a/m.js\n+++ b/m.js\n@@ -1,3 +1,6 @@\n"
        "+/** opener whose */ is in context\n ctx();\n+evil();\n+evil();\n ctx();\n+ closer */\n"
    )
    monkeypatch.setattr(vt, "_git_diff", lambda extra: "M\0m.js\0" if "--name-status" in extra else patch)
    _, added, loc, deleted = vt.get_diff("HEAD")
    assert vt.RUN_STARTS[("+", "m.js")] == {0, 1, 3}
    assert vt.loc_breakdown(added, loc, deleted)["effective"] >= 2  # the two evil() lines
