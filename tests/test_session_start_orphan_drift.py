"""Tests for session_start.orphan_drift() — the orphan-memory-file counter.

An orphan is a memory markdown file whose `pg_id` does not resolve to a live
entity, INCLUDING a file that carries no `pg_id` at all. That definition, the
frontmatter reader and the batched lookup all come from the optional backend's
`scripts/memory_dedup.py`, and are reused rather than re-implemented.

The backend is a separate checkout, so the unit tests below stub the loader
with readers that honour the real contracts. The real wiring is exercised in
TestRealWiring, which runs only where a backend checkout is present.
"""

from __future__ import annotations

import inspect
import re
import types
from pathlib import Path

import pytest
import session_start as ss

# The real loader, captured before any fixture stubs it.
REAL_LOAD_MEMORY_DEDUP = ss._load_memory_dedup

LIVE_ID = "11111111-1111-4111-8111-111111111111"
DEAD_ID = "22222222-2222-4222-8222-222222222222"


def faithful_read_fm(p: Path) -> dict:
    """Mirror of memory_dedup.read_fm's pg_id handling: frontmatter-scoped,
    quotes stripped, `pg_id:` with no value reads as None."""
    t = p.read_text(errors="replace")
    m = re.match(r"^---\n(.*?)\n---", t, re.S)
    blk = m.group(1) if m else t[:800]
    mm = re.search(r"^\s*pg_id\s*:\s*(.+)$", blk, re.M)
    return {"pgid": mm.group(1).strip().strip('"').strip("'") if mm else None}


def faithful_fetch(live: dict[str, tuple[str, float]] | None = None):
    """A stub that honours the REAL fetch_thermal_by_pgid contract.

    The real helper returns None — never {} — for an empty or all-malformed id
    list. A stub returning {} there would hide a bug where a tree of entirely
    pg_id-less files reports nothing. Stubs must not be more generous than the
    real thing.
    """

    def _fetch(ids, **_kwargs):
        valid = [i for i in ids if re.fullmatch(r"[0-9a-fA-F-]{36}", i)]
        if not valid:
            return None
        return {k: v for k, v in (live or {}).items() if k in valid}

    return _fetch


@pytest.fixture
def stub_backend(monkeypatch) -> None:
    monkeypatch.setattr(ss, "BRAIN_ENABLED", True)
    # Never consult the real env/dotfiles of whoever runs the suite.
    monkeypatch.setattr(ss, "resolve_connection_string", lambda *a: "postgresql://test-db")
    fake = types.SimpleNamespace(read_fm=faithful_read_fm)
    monkeypatch.setattr(ss, "_load_memory_dedup", lambda: fake)


def _write_memory(dir_path: Path, name: str, pg_id: str | None) -> None:
    """Write a minimal memory file, with or without a pg_id in frontmatter."""
    dir_path.mkdir(parents=True, exist_ok=True)
    pg_line = f"pg_id: {pg_id}\n" if pg_id else ""
    (dir_path / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: d\ntype: reference\n{pg_line}---\n\nbody\n"
    )


def _projects_root(tmp_path: Path, layout: dict[str, list[tuple[str, str | None]]]) -> Path:
    """Build a fake ~/.claude/projects tree: {dir_name: [(file_name, pg_id), ...]}."""
    root = tmp_path / "projects"
    for dir_name, files in layout.items():
        for file_name, pg_id in files:
            _write_memory(root / dir_name / "memory", file_name, pg_id)
    return root


@pytest.mark.usefixtures("stub_backend")
class TestOrphanDrift:
    def test_no_orphans_emits_nothing(self, tmp_path):
        root = _projects_root(tmp_path, {"-proj-a": [("a", LIVE_ID)]})
        msgs = ss.orphan_drift(projects_root=root, fetch=faithful_fetch({LIVE_ID: ("HOT", 1.0)}))
        assert msgs == []

    def test_counts_dead_pointers_and_dirs(self, tmp_path):
        root = _projects_root(
            tmp_path,
            {
                "-proj-a": [("live", LIVE_ID), ("dead", DEAD_ID)],
                "-proj-b": [("dead2", DEAD_ID)],
            },
        )
        msgs = ss.orphan_drift(projects_root=root, fetch=faithful_fetch({LIVE_ID: ("HOT", 1.0)}))
        assert len(msgs) == 1
        assert "2 dead pg_id" in msgs[0]
        assert "across 2 dir(s)" in msgs[0]
        assert "memory_dedup.py" in msgs[0]

    def test_missing_pg_id_counts_as_orphan(self, tmp_path):
        root = _projects_root(tmp_path, {"-proj-a": [("nopgid", None)]})
        msgs = ss.orphan_drift(projects_root=root, fetch=faithful_fetch())
        assert len(msgs) == 1
        assert "1 without pg_id" in msgs[0]

    def test_reports_the_two_causes_separately(self, tmp_path):
        """A dead pointer needs triage; a missing pg_id needs a backfill."""
        root = _projects_root(
            tmp_path, {"-proj-a": [("dead", DEAD_ID), ("nopgid", None), ("live", LIVE_ID)]}
        )
        msgs = ss.orphan_drift(projects_root=root, fetch=faithful_fetch({LIVE_ID: ("HOT", 1.0)}))
        assert len(msgs) == 1
        assert "1 dead pg_id, 1 without pg_id across 1 dir(s)" in msgs[0]

    def test_omits_a_cause_with_zero_count(self, tmp_path):
        root = _projects_root(tmp_path, {"-proj-a": [("dead", DEAD_ID)]})
        msgs = ss.orphan_drift(projects_root=root, fetch=faithful_fetch())
        assert "1 dead pg_id" in msgs[0]
        assert "without pg_id" not in msgs[0]

    def test_all_files_lack_pg_id_still_reports(self, tmp_path):
        """The maximum-drift case must NOT be the silent one: the real fetch
        returns None for an empty id list, and `missing` needs no DB."""
        root = _projects_root(tmp_path, {"-proj-a": [("x", None), ("y", None)]})
        msgs = ss.orphan_drift(projects_root=root, fetch=faithful_fetch())
        assert len(msgs) == 1
        assert "2 without pg_id" in msgs[0]

    def test_db_unreachable_still_reports_missing_half(self, tmp_path):
        root = _projects_root(tmp_path, {"-proj-a": [("dead", DEAD_ID), ("nopgid", None)]})
        msgs = ss.orphan_drift(projects_root=root, fetch=lambda ids, **kw: None)
        assert len(msgs) == 1
        assert "1 without pg_id" in msgs[0]
        assert "dead pg_id" not in msgs[0]

    def test_all_ids_unresolved_is_treated_as_wrong_datasource(self, tmp_path):
        """A reachable-but-wrong database answers {} and every live id reads as
        dead, so a 100% dead rate over a real corpus is reported as a
        datasource problem, not as drift."""
        ids = [f"{i:08d}-1111-4111-8111-111111111111" for i in range(8)]
        root = _projects_root(tmp_path, {"-proj-a": [(f"m{i}", pid) for i, pid in enumerate(ids)]})
        msgs = ss.orphan_drift(projects_root=root, fetch=faithful_fetch())
        assert len(msgs) == 1
        assert "suspected wrong datasource" in msgs[0]
        assert "dead pg_id" not in msgs[0]

    def test_wrong_datasource_alarm_keeps_the_filesystem_causes(self, tmp_path):
        ids = [f"{i:08d}-1111-4111-8111-111111111111" for i in range(8)]
        layout = [(f"m{i}", pid) for i, pid in enumerate(ids)]
        layout += [("nopgid", None), ("junk", "not-a-uuid")]
        root = _projects_root(tmp_path, {"-proj-a": layout})
        msgs = ss.orphan_drift(projects_root=root, fetch=faithful_fetch())
        assert len(msgs) == 2
        assert "suspected wrong datasource" in msgs[0]
        assert "1 invalid pg_id, 1 without pg_id across 1 dir(s)" in msgs[1]
        assert "dead pg_id" not in msgs[1]

    def test_an_unreadable_fetch_signature_skips_the_lookup(self, tmp_path, capsys):
        root = _projects_root(tmp_path, {"-proj-a": [("dead", DEAD_ID), ("nopgid", None)]})
        calls = []

        def fetch(ids, **kw):
            calls.append(kw)
            return {}

        fetch.__signature__ = "not a Signature"  # inspect.signature raises ValueError
        msgs = ss.orphan_drift(projects_root=root, fetch=fetch)
        assert calls == []
        assert "1 without pg_id" in msgs[0]
        assert "dead pg_id" not in msgs[0]
        assert "fetch signature unreadable" in capsys.readouterr().err

    def test_wrong_datasource_guard_boundary(self, tmp_path):
        """At exactly MIN_IDS_FOR_DRIFT_ALARM the guard must NOT fire (>, not >=)."""
        ids = [f"{i:08d}-1111-4111-8111-111111111111" for i in range(ss.MIN_IDS_FOR_DRIFT_ALARM)]
        root = _projects_root(tmp_path, {"-proj-a": [(f"m{i}", pid) for i, pid in enumerate(ids)]})
        msgs = ss.orphan_drift(projects_root=root, fetch=faithful_fetch())
        assert f"{len(ids)} dead pg_id" in msgs[0]

    def test_empty_pg_id_value_counts_as_missing(self, tmp_path):
        d = tmp_path / "projects" / "-proj-a" / "memory"
        d.mkdir(parents=True)
        (d / "blank.md").write_text("---\nname: blank\ntype: reference\npg_id:\n---\n\nbody\n")
        msgs = ss.orphan_drift(projects_root=tmp_path / "projects", fetch=faithful_fetch())
        assert "1 without pg_id" in msgs[0]

    def test_malformed_pg_id_is_reported_as_invalid(self, tmp_path):
        """A non-uuid id never reaches the backend; it is its own cause."""
        d = tmp_path / "projects" / "-proj-a" / "memory"
        d.mkdir(parents=True)
        (d / "junk.md").write_text("---\nname: junk\ntype: reference\npg_id: not-a-uuid\n---\n\nx\n")
        msgs = ss.orphan_drift(projects_root=tmp_path / "projects", fetch=faithful_fetch())
        assert len(msgs) == 1
        assert "1 invalid pg_id" in msgs[0]
        assert "dead pg_id" not in msgs[0]

    def test_one_uuid_shaped_bad_id_cannot_silence_the_batch(self, tmp_path):
        """36 hex chars passes memory_dedup's loose filter but fails Postgres's
        ::uuid cast, failing the whole batch. The hook must keep it out."""
        bad = "a" * 36
        root = _projects_root(tmp_path, {"-proj-a": [("bad", bad), ("dead", DEAD_ID)]})
        sent = []

        def fetch(ids, **kw):
            sent.extend(ids)
            return {}

        msgs = ss.orphan_drift(projects_root=root, fetch=fetch)
        assert sent == [DEAD_ID]
        assert "1 dead pg_id, 1 invalid pg_id across 1 dir(s)" in msgs[0]

    def test_no_connection_string_skips_the_lookup(self, tmp_path, monkeypatch, capsys):
        """Without a URL the fetch would fall back to memory_dedup's own
        resolver and might query another database: dead is UNKNOWN instead."""
        monkeypatch.setattr(ss, "resolve_connection_string", lambda *a: None)
        root = _projects_root(tmp_path, {"-proj-a": [("dead", DEAD_ID), ("nopgid", None)]})
        calls = []

        def fetch(ids, timeout=30, url=None):
            calls.append(url)
            return {}

        msgs = ss.orphan_drift(projects_root=root, fetch=fetch)
        assert calls == []
        assert "1 without pg_id" in msgs[0]
        assert "dead pg_id" not in msgs[0]
        assert "no connection string resolved" in capsys.readouterr().err

    def test_a_kwargs_fetch_still_receives_the_url(self, tmp_path):
        root = _projects_root(tmp_path, {"-proj-a": [("a", LIVE_ID)]})
        seen = {}

        def fetch(ids, **kw):
            seen.update(kw)
            return {LIVE_ID: ("HOT", 1.0)}

        assert ss.orphan_drift(projects_root=root, fetch=fetch) == []
        assert seen == {"timeout": ss.ORPHAN_TIMEOUT_S, "url": "postgresql://test-db"}

    def test_one_unreadable_file_does_not_hide_the_rest(self, tmp_path, monkeypatch):
        root = _projects_root(
            tmp_path, {"-proj-a": [("broken", DEAD_ID), ("nopgid", None)]}
        )

        def read_fm(p):
            if p.name == "broken.md":
                raise PermissionError("unreadable")
            return faithful_read_fm(p)

        monkeypatch.setattr(ss, "_load_memory_dedup", lambda: types.SimpleNamespace(read_fm=read_fm))
        msgs = ss.orphan_drift(projects_root=root, fetch=faithful_fetch())
        assert len(msgs) == 1
        assert "1 without pg_id across 1 dir(s)" in msgs[0]

    def test_same_pg_id_in_two_dirs_counts_both_files(self, tmp_path):
        root = _projects_root(
            tmp_path, {"-proj-a": [("dup", DEAD_ID)], "-proj-b": [("dup", DEAD_ID)]}
        )
        msgs = ss.orphan_drift(projects_root=root, fetch=faithful_fetch())
        assert "2 dead pg_id" in msgs[0]
        assert "across 2 dir(s)" in msgs[0]

    def test_db_unreachable_emits_nothing_not_zero(self, tmp_path):
        """None means UNKNOWN, never 'no orphans'."""
        root = _projects_root(tmp_path, {"-proj-a": [("dead", DEAD_ID)]})
        msgs = ss.orphan_drift(projects_root=root, fetch=lambda ids: None)
        assert msgs == []

    def test_missing_projects_root_is_not_an_error(self, tmp_path):
        assert ss.orphan_drift(projects_root=tmp_path / "nope", fetch=faithful_fetch()) == []

    def test_ignores_memory_index_file(self, tmp_path):
        root = _projects_root(tmp_path, {"-proj-a": [("a", LIVE_ID)]})
        (root / "-proj-a" / "memory" / "MEMORY.md").write_text("# Memory Index\n\n- ref: a — d\n")
        msgs = ss.orphan_drift(projects_root=root, fetch=faithful_fetch({LIVE_ID: ("HOT", 1.0)}))
        assert msgs == []

    def test_a_raising_fetch_is_contained(self, tmp_path, capsys):
        root = _projects_root(tmp_path, {"-proj-a": [("dead", DEAD_ID)]})

        def boom(ids, **kw):
            raise RuntimeError("db exploded")

        assert ss.orphan_drift(projects_root=root, fetch=boom) == []
        assert "orphan_drift failed" in capsys.readouterr().err

    def test_fetch_receives_the_hooks_url_and_timeout(self, tmp_path):
        """The hook's resolver and memory_dedup's read different files; passing
        `url` keeps both looking at the same database."""
        root = _projects_root(tmp_path, {"-proj-a": [("a", LIVE_ID)]})
        seen = {}

        def fetch(ids, timeout=30, url=None):
            seen.update(timeout=timeout, url=url)
            return {LIVE_ID: ("HOT", 1.0)}

        assert ss.orphan_drift(projects_root=root, fetch=fetch) == []
        assert seen == {"timeout": ss.ORPHAN_TIMEOUT_S, "url": "postgresql://test-db"}


@pytest.mark.usefixtures("stub_backend")
class TestGating:
    def test_brain_disabled_never_touches_the_backend(self, monkeypatch):
        monkeypatch.setattr(ss, "BRAIN_ENABLED", False)
        # Record rather than raise: orphan_drift's blanket except would swallow
        # an AssertionError and the test would pass with the gate removed.
        calls = []
        monkeypatch.setattr(ss, "_load_memory_dedup", lambda: calls.append(1))
        assert ss.orphan_drift() == []
        assert calls == []

    def test_an_import_failure_is_logged_and_silent(self, monkeypatch, tmp_path, capsys):
        scripts = tmp_path / "repo" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "memory_dedup.py").write_text("raise RuntimeError('broken backend')\n")
        monkeypatch.setattr(ss, "MEMORY_REPO_DIR", tmp_path / "repo")
        monkeypatch.delitem(ss.sys.modules, "memory_dedup", raising=False)
        monkeypatch.setattr(ss.sys, "path", list(ss.sys.path))
        assert REAL_LOAD_MEMORY_DEDUP() is None
        assert "memory_dedup import failed: RuntimeError" in capsys.readouterr().err

    def test_no_backend_checkout_is_silent(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ss, "BRAIN_ENABLED", True)
        monkeypatch.setattr(ss, "_load_memory_dedup", lambda: None)
        root = _projects_root(tmp_path, {"-proj-a": [("x", None)]})
        assert ss.orphan_drift(projects_root=root) == []


def _real_backend_present() -> bool:
    return (ss.MEMORY_REPO_DIR / "scripts" / "memory_dedup.py").is_file()


@pytest.mark.skipif(
    not _real_backend_present(),
    reason="no memory backend checkout at MEMORY_REPO_DIR; CI has none",
)
class TestRealWiring:
    """The stubbed tests prove the logic, not the import that runs in
    production. These exercise the real loader against a synthetic tree."""

    def test_real_import_and_glob_are_load_bearing(self, tmp_path):
        md = ss._load_memory_dedup()
        assert md is not None, "memory_dedup no longer importable from the hook"
        assert callable(md.read_fm)
        assert callable(md.fetch_thermal_by_pgid)
        # If the backend ever drops `url`, the hook silently stops pinning the
        # database it queries — the failure `url` exists to prevent.
        assert "url" in inspect.signature(md.fetch_thermal_by_pgid).parameters
        root = tmp_path / "projects"
        _write_memory(root / "-proj-a" / "memory", "real", LIVE_ID)
        assert list(ss._iter_memory_pgids(root, md.read_fm)) == [(LIVE_ID, "-proj-a")]

    def test_real_fetch_rejects_empty_list_with_none(self):
        """Pins the contract faithful_fetch imitates."""
        assert ss._load_memory_dedup().fetch_thermal_by_pgid([]) is None
