"""Tests for session_start.py's pending section.

Scope is deliberately narrow: the pending brief and its fail-open behaviour,
which is the part that must never hard-fail a session start.
"""

from __future__ import annotations

import session_start as ss
import pytest


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ── DB tier (BRAIN_ENABLED=true) ─────────────────────────────────────────────


class TestPendingSectionFromDb:
    @pytest.fixture(autouse=True)
    def _db_tier(self, monkeypatch, tmp_path) -> None:
        """Exercise the DB tier only.

        BRAIN_ENABLED is read at import time, so patch the module attribute.
        PENDING_FILE points at a path that provably doesn't exist, so a
        regression that short-circuits the DB tier can't silently fall through
        to reading the real pending.md on whoever's machine runs the suite.
        shutil.which is stubbed so these tests do not depend on psql being
        installed on the machine (or the CI image) running them.
        """
        monkeypatch.setattr(ss, "BRAIN_ENABLED", True)
        monkeypatch.setattr(ss, "PENDING_FILE", tmp_path / "unreachable.md")
        monkeypatch.setattr(ss.shutil, "which", lambda _: "/usr/bin/psql")

    def test_renders_titles_with_priority_prefix(self, monkeypatch) -> None:
        rows = "3\thigh\tFix the thing\n3\tlow\tDo the other\n"
        monkeypatch.setattr(ss, "resolve_connection_string", lambda *a: "postgresql://x")
        monkeypatch.setattr(
            ss.subprocess, "run", lambda *a, **k: _FakeCompleted(0, rows)
        )
        out = ss.pending_section()
        assert "## Pending (3 open, showing 2)" in out
        assert "- [high] Fix the thing" in out
        assert "- [low] Do the other" in out

    def test_omits_bodies_entirely(self, monkeypatch) -> None:
        rows = "1\thigh\tShort title\n"
        monkeypatch.setattr(ss, "resolve_connection_string", lambda *a: "postgresql://x")
        monkeypatch.setattr(
            ss.subprocess, "run", lambda *a, **k: _FakeCompleted(0, rows)
        )
        out = ss.pending_section()
        assert len(out) < 200
        assert "\t" not in out

    def test_reports_none_when_the_queue_is_empty(self, monkeypatch) -> None:
        monkeypatch.setattr(ss, "resolve_connection_string", lambda *a: "postgresql://x")
        monkeypatch.setattr(ss.subprocess, "run", lambda *a, **k: _FakeCompleted(0, ""))
        assert ss.pending_section().strip() == "## Pending\nNone."

    def test_query_limits_and_orders_by_priority(self, monkeypatch) -> None:
        captured = {}

        def fake_run(args, **kwargs):
            captured["sql"] = args[-1]
            captured["timeout"] = kwargs.get("timeout")
            return _FakeCompleted(0, "")

        monkeypatch.setattr(ss, "resolve_connection_string", lambda *a: "postgresql://x")
        monkeypatch.setattr(ss.subprocess, "run", fake_run)
        ss.pending_section()

        assert f"LIMIT {ss.PENDING_BRIEF_LIMIT}" in captured["sql"]
        assert "status = 'open'" in captured["sql"]
        assert "WHEN 'high' THEN 0" in captured["sql"]
        # Escapes must reach Postgres literally, not as real tab/newline bytes.
        assert r"E'\t'" in captured["sql"]
        assert captured["timeout"] == ss.PENDING_TIMEOUT_S

    def test_a_title_containing_a_tab_does_not_shift_columns(self, monkeypatch) -> None:
        # A residual tab in the last field folds into the title, because the
        # split is bounded at maxsplit=2.
        rows = "1\thigh\ttitle\twith tab\n"
        monkeypatch.setattr(ss, "resolve_connection_string", lambda *a: "postgresql://x")
        monkeypatch.setattr(
            ss.subprocess, "run", lambda *a, **k: _FakeCompleted(0, rows)
        )
        out = ss.pending_section()
        assert "## Pending (1 open, showing 1)" in out
        assert "- [high] title" in out


# ── fail-open ────────────────────────────────────────────────────────────────


class TestPendingSectionFailsOpen:
    @pytest.fixture(autouse=True)
    def _db_tier(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(ss, "BRAIN_ENABLED", True)
        monkeypatch.setattr(ss, "PENDING_FILE", tmp_path / "nope.md")
        monkeypatch.setattr(ss, "resolve_connection_string", lambda *a: "postgresql://u:p@h/db")
        monkeypatch.setattr(ss.shutil, "which", lambda _: "/usr/bin/psql")

    def test_a_raising_query_never_propagates(self, monkeypatch, capsys) -> None:
        def boom(*a, **k):
            raise OSError("psql blew up")

        monkeypatch.setattr(ss.subprocess, "run", boom)
        assert ss.pending_section().strip() == "## Pending\nNone."  # must not raise
        # Assert the DB tier was actually entered and swallowed the error —
        # without this the test also passes if the DB tier is never reached,
        # because the file tier returns the identical string.
        assert "pending query failed: OSError" in capsys.readouterr().err

    def test_a_timeout_never_propagates(self, monkeypatch, capsys) -> None:
        def slow(*a, **k):
            raise ss.subprocess.TimeoutExpired(cmd="psql", timeout=5)

        monkeypatch.setattr(ss.subprocess, "run", slow)
        assert ss.pending_section().strip() == "## Pending\nNone."
        assert "pending query failed: TimeoutExpired" in capsys.readouterr().err

    def test_the_exception_text_is_never_echoed(self, monkeypatch, capsys) -> None:
        """A timeout must not print the credential-bearing argv.

        subprocess.TimeoutExpired.__str__ embeds the full command, so logging
        `e` rather than `type(e).__name__` would write a live password into
        the session transcript.
        """

        def slow(*a, **k):
            raise ss.subprocess.TimeoutExpired(
                cmd=["psql", "postgresql://user:hunter2@host/db"], timeout=5
            )

        monkeypatch.setattr(ss.subprocess, "run", slow)
        ss.pending_section()
        assert "hunter2" not in capsys.readouterr().err

    def test_the_password_is_never_placed_in_argv(self, monkeypatch) -> None:
        dsn = "postgresql://alice:hunter2@db.example.com:6543/mydb?sslmode=require"
        monkeypatch.setattr(ss, "resolve_connection_string", lambda *a: dsn)
        captured = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            captured["env"] = kwargs.get("env") or {}
            return _FakeCompleted(0, "")

        monkeypatch.setattr(ss.subprocess, "run", fake_run)
        ss.pending_section()

        # argv is world-readable via `ps` — the credential must travel in env.
        assert "hunter2" not in " ".join(captured["args"])
        assert not any("postgresql://" in a for a in captured["args"])
        assert captured["env"]["PGPASSWORD"] == "hunter2"
        assert captured["env"]["PGUSER"] == "alice"
        assert captured["env"]["PGHOST"] == "db.example.com"
        assert captured["env"]["PGPORT"] == "6543"
        assert captured["env"]["PGDATABASE"] == "mydb"
        assert captured["env"]["PGSSLMODE"] == "require"

    def test_a_percent_encoded_password_is_decoded(self, monkeypatch) -> None:
        # libpq takes the raw value from PGPASSWORD, so the URL-encoded form
        # must be unquoted on the way in or authentication fails.
        monkeypatch.setattr(
            ss, "resolve_connection_string", lambda *a: "postgresql://u:p%40ss@h:5432/d"
        )
        captured = {}

        def fake_run(args, **kwargs):
            captured["env"] = kwargs.get("env") or {}
            return _FakeCompleted(0, "")

        monkeypatch.setattr(ss.subprocess, "run", fake_run)
        ss.pending_section()
        assert captured["env"]["PGPASSWORD"] == "p@ss"

    def test_ambient_pg_vars_cannot_redirect_the_connection(
        self, monkeypatch
    ) -> None:
        # PGHOSTADDR outranks PGHOST in libpq, so an inherited one would send
        # psql to a different server than the URL names.
        monkeypatch.setenv("PGHOSTADDR", "10.0.0.1")
        monkeypatch.setenv("PGSERVICE", "someservice")
        monkeypatch.setenv("PGDATABASE", "stale_db")
        monkeypatch.setattr(
            ss, "resolve_connection_string", lambda *a: "postgresql://u:p@real.host:5432/realdb"
        )
        captured = {}

        def fake_run(args, **kwargs):
            captured["env"] = kwargs.get("env") or {}
            return _FakeCompleted(0, "")

        monkeypatch.setattr(ss.subprocess, "run", fake_run)
        ss.pending_section()

        assert "PGHOSTADDR" not in captured["env"]
        assert "PGSERVICE" not in captured["env"]
        assert captured["env"]["PGHOST"] == "real.host"
        assert captured["env"]["PGDATABASE"] == "realdb"
        # Non-PG environment must still pass through.
        assert "PATH" in captured["env"]

    def test_an_unparseable_connection_string_falls_back(
        self, monkeypatch, capsys
    ) -> None:
        monkeypatch.setattr(ss, "resolve_connection_string", lambda *a: "not-a-url")

        def explode(*a, **k):
            raise AssertionError("must not shell out on an unparseable URL")

        monkeypatch.setattr(ss.subprocess, "run", explode)
        assert ss.pending_section().strip() == "## Pending\nNone."
        assert "unparseable connection string" in capsys.readouterr().err

    def test_a_missing_psql_falls_back_instead_of_raising(
        self, monkeypatch, capsys
    ) -> None:
        monkeypatch.setattr(ss.shutil, "which", lambda _: None)

        def explode(*a, **k):
            raise AssertionError("must not shell out when psql is absent")

        monkeypatch.setattr(ss.subprocess, "run", explode)
        assert ss.pending_section().strip() == "## Pending\nNone."
        assert "psql not on PATH" in capsys.readouterr().err


# ── file tier (the zero-setup default) ───────────────────────────────────────


class TestPendingSectionFromFile:
    @pytest.fixture(autouse=True)
    def _file_tier(self, monkeypatch) -> None:
        monkeypatch.setattr(ss, "BRAIN_ENABLED", False)

    def test_does_not_query_the_db_when_brain_is_disabled(
        self, monkeypatch, tmp_path
    ) -> None:
        def explode(*a, **k):
            raise AssertionError("subprocess.run must not be called")

        monkeypatch.setattr(ss.subprocess, "run", explode)
        monkeypatch.setattr(ss, "PENDING_FILE", tmp_path / "absent.md")
        assert ss.pending_section().strip() == "## Pending\nNone."

    def test_reads_entries_from_the_file(self, monkeypatch, tmp_path) -> None:
        legacy = tmp_path / "pending.md"
        legacy.write_text(
            "# Pending Improvements\n"
            "# some preamble\n"
            "## [2026-01-01] Legacy entry\n"
            "body text\n"
        )
        monkeypatch.setattr(ss, "PENDING_FILE", legacy)
        out = ss.pending_section()
        assert "Legacy entry" in out
        # The single-`#` preamble is stripped, `##` entry headers survive.
        assert "Pending Improvements" not in out
        assert "some preamble" not in out

    def test_an_empty_file_reports_none(self, monkeypatch, tmp_path) -> None:
        legacy = tmp_path / "pending.md"
        legacy.write_text("# Pending Improvements\n")
        monkeypatch.setattr(ss, "PENDING_FILE", legacy)
        assert ss.pending_section().strip() == "## Pending\nNone."


class TestBriefIntegration:
    def test_brief_includes_the_pending_section(self, monkeypatch) -> None:
        monkeypatch.setattr(ss, "pending_section", lambda: "## Pending\nSENTINEL\n")
        brief = ss.build_brief(ss.make_fresh_state(), resumed=False)
        assert "SENTINEL" in brief
