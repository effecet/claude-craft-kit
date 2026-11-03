"""Migration lines copied verbatim from a sibling must not inflate the tier.

This repo supersedes a migration by reproducing it — 0014 is 0013 plus two
lines. Counted naively that read as a 240-LOC change, escalating a ten-line fix
to Full tier and a proposed six-agent review. The script was reporting a number
true about the diff and false about the work, and the whole session bent around
it.

The risk running the other way is worse and is tested here explicitly: a
discount that cancels a migration against ITSELF would report 0 LOC and route a
real schema change straight past review.
"""

from __future__ import annotations

import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

import validate_tier as vt  # noqa: E402


def _drizzle(tmp_path: Path, **files: str) -> None:
    d = tmp_path / "drizzle"
    d.mkdir(exist_ok=True)
    for name, body in files.items():
        (d / name).write_text(body, encoding="utf-8")


class TestEffectiveLoc:
    def test_no_migration_files_is_a_passthrough(self) -> None:
        assert vt.effective_loc({"src/thermal.ts": ["a", "b"]}, 2) == 2

    def test_discounts_lines_present_in_a_sibling_migration(self, tmp_path, monkeypatch) -> None:
        _drizzle(tmp_path, **{"0013_old.sql": "SELECT 1;\nSELECT 2;\nSELECT 3;\n"})
        monkeypatch.chdir(tmp_path)
        added = {"drizzle/0014_new.sql": ["SELECT 1;", "SELECT 2;", "SELECT 3;", "SELECT 4;"]}
        assert vt.effective_loc(added, 4) == 1

    def test_a_genuinely_new_migration_is_not_discounted(self, tmp_path, monkeypatch) -> None:
        _drizzle(tmp_path, **{"0013_old.sql": "SELECT 1;\n"})
        monkeypatch.chdir(tmp_path)
        added = {"drizzle/0014_new.sql": ["CREATE TABLE a();", "CREATE TABLE b();"]}
        assert vt.effective_loc(added, 2) == 2

    def test_does_not_discount_a_migration_against_itself(self, tmp_path, monkeypatch) -> None:
        """The changed file is on disk too — it must be excluded from the corpus.

        A corpus that included it would match every added line and report 0 LOC,
        sending a real schema change through with no review at all. That failure
        is strictly worse than the over-count this function fixes, so it gets its
        own test rather than riding on the implementation being obvious.
        """
        _drizzle(tmp_path, **{"0014_new.sql": "CREATE TABLE a();\nCREATE TABLE b();\n"})
        monkeypatch.chdir(tmp_path)
        added = {"drizzle/0014_new.sql": ["CREATE TABLE a();", "CREATE TABLE b();"]}
        assert vt.effective_loc(added, 2) == 2

    def test_non_migration_sql_is_not_discounted(self, tmp_path, monkeypatch) -> None:
        _drizzle(tmp_path, **{"0013_old.sql": "SELECT 1;\n"})
        monkeypatch.chdir(tmp_path)
        assert vt.effective_loc({"scripts/seed.sql": ["SELECT 1;"]}, 1) == 1

    def test_missing_drizzle_dir_is_a_passthrough(self, tmp_path, monkeypatch) -> None:
        """Runs from any repo, not just this one — no drizzle/ means no discount."""
        monkeypatch.chdir(tmp_path)
        assert vt.effective_loc({"drizzle/0014_new.sql": ["SELECT 1;"]}, 1) == 1

    def test_deletions_are_never_discounted(self, tmp_path, monkeypatch) -> None:
        """raw_loc counts removed lines too; only ADDED lines can be copies.

        A migration that deletes 50 lines and adds 3 copied ones is 50 LOC of
        real work. Discounting against the raw total rather than the added set
        would erase it.
        """
        _drizzle(tmp_path, **{"0013_old.sql": "SELECT 1;\n"})
        monkeypatch.chdir(tmp_path)
        # raw_loc 51 = 50 deletions + 1 addition; the addition is a copy.
        assert vt.effective_loc({"drizzle/0014_new.sql": ["SELECT 1;"]}, 51) == 50
