"""Tests for session_start.canary_liveness, the brief's stale-heartbeat check.

A scheduled job that stops running usually stops silently. If it writes the
current Unix time to CRAFT_CANARY_FILE on every run, the session brief reads
that stamp and warns once it is older than CRAFT_CANARY_MAX_AGE_H.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

import session_start as ss  # noqa: E402

NOW = 1_800_000_000


@pytest.fixture
def stamp(tmp_path: Path) -> Path:
    return tmp_path / "heartbeat"


def test_silent_when_not_configured(monkeypatch) -> None:
    monkeypatch.setattr(ss, "CANARY_FILE", None)
    assert ss.canary_liveness(now=NOW) == []


def test_fresh_stamp_is_silent(stamp: Path) -> None:
    stamp.write_text(str(NOW - 3_500))
    assert ss.canary_liveness(stamp, 3, NOW) == []


def test_stale_stamp_warns_with_its_age(stamp: Path) -> None:
    stamp.write_text(str(NOW - 5 * 3600))
    msgs = ss.canary_liveness(stamp, 3, NOW)
    assert len(msgs) == 1
    assert "5h" in msgs[0] and "limit 3h" in msgs[0]


def test_the_limit_is_inclusive(stamp: Path) -> None:
    stamp.write_text(str(NOW - 3 * 3600))
    assert ss.canary_liveness(stamp, 3, NOW) == []


def test_a_custom_limit_applies(stamp: Path) -> None:
    stamp.write_text(str(NOW - 5 * 3600))
    assert ss.canary_liveness(stamp, 24, NOW) == []


def test_missing_stamp_warns(stamp: Path) -> None:
    msgs = ss.canary_liveness(stamp, 3, NOW)
    assert len(msgs) == 1
    assert "no run recorded" in msgs[0]


def test_unreadable_stamp_warns(stamp: Path) -> None:
    stamp.write_text("not a number")
    assert "no run recorded" in ss.canary_liveness(stamp, 3, NOW)[0]


def test_future_stamp_is_not_healthy(stamp: Path) -> None:
    # A stamp from the future would otherwise read as fresh forever.
    stamp.write_text(str(NOW + ss.CANARY_SKEW_S + 60))
    assert "in the future" in ss.canary_liveness(stamp, 3, NOW)[0]


def test_small_clock_skew_is_tolerated(stamp: Path) -> None:
    stamp.write_text(str(NOW + 60))
    assert ss.canary_liveness(stamp, 3, NOW) == []


def test_module_defaults_come_from_the_settings(monkeypatch, stamp: Path) -> None:
    stamp.write_text(str(NOW - 5 * 3600))
    monkeypatch.setattr(ss, "CANARY_FILE", stamp)
    monkeypatch.setattr(ss, "CANARY_MAX_AGE_H", 4.0)
    assert "limit 4h" in ss.canary_liveness(now=NOW)[0]


def test_wired_into_the_brief() -> None:
    src = (Path(__file__).resolve().parent.parent / "hooks" / "session_start.py").read_text()
    assert "canary_liveness()" in src
