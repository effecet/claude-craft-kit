"""session_start.py bounds its own stdout under the hook output cap.

Claude Code caps a hook's plain stdout at 10,000 characters; over it the model
sees a file path and a 2,000-char preview and is not asked to read the rest, so
whatever comes last silently vanishes. render_stdout() keeps the payload under
STDOUT_BUDGET: low-value groups degrade to a one-line pointer first, alert
groups never degrade, and a meter line makes growth visible.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

import session_start as ss  # noqa: E402

FOOTER_MARK = "Read "


def _groups(**overrides):
    groups = [
        ss.MsgGroup("evolve", ["evolve: 3 instincts"], rank=0),
        ss.MsgGroup("decay", ["decay: 12 rows"], rank=1),
        ss.MsgGroup("hook_drift", ["⚠️ hook drift: settings.json"]),
    ]
    for g in groups:
        if g.name in overrides:
            g.lines = overrides[g.name]
    return groups


def _big(prefix, n, width=80):
    return [f"{prefix} {i} " + "x" * width for i in range(n)]


def test_no_messages_is_header_blank_footer_meter(tmp_path):
    out = ss.render_stdout([], resumed=False, overflow_file=tmp_path / "f.txt")
    lines = out.splitlines()
    assert lines[:3] == ["Session started.", f"Brief written to {ss.BRIEF_FILE}.", ""]
    assert lines[3].startswith(FOOTER_MARK)
    assert (
        lines[-1]
        == f"stdout: {len(out) - len(lines[-1]) - 1:,} / {ss.STDOUT_BUDGET:,} chars"
    )


def test_small_output_is_unchanged_plus_a_meter(tmp_path):
    out = ss.render_stdout(
        _groups(), resumed=False, overflow_file=tmp_path / "full.txt"
    )
    assert "evolve: 3 instincts" in out and "decay: 12 rows" in out
    assert "⚠️ hook drift: settings.json" in out
    assert out.splitlines()[-1].startswith("stdout: ")
    assert "dropped" not in out.splitlines()[-1]
    assert not (tmp_path / "full.txt").exists()


def test_a_fitting_run_clears_a_stale_overflow_file(tmp_path):
    stale = tmp_path / "full.txt"
    stale.write_text("yesterday's overflow")
    ss.render_stdout(_groups(), resumed=False, overflow_file=stale)
    assert not stale.exists()


def test_oversized_degradable_group_becomes_a_pointer_and_the_meter_names_it(tmp_path):
    full = tmp_path / "full.txt"
    out = ss.render_stdout(
        _groups(evolve=_big("evolve line", 200)), resumed=False, overflow_file=full
    )
    assert len(out) <= ss.STDOUT_BUDGET
    assert "evolve line 0" not in out
    assert "evolve: 200 lines omitted" in out and str(full) in out
    assert "dropped: evolve" in out.splitlines()[-1]
    assert "evolve line 199" in full.read_text()  # nothing is lost
    assert "⚠️ hook drift: settings.json" in out
    assert FOOTER_MARK in out


def test_pointer_counts_real_lines_not_list_entries(tmp_path):
    multi = ["a\nb\nc " + "x" * 200] * 60  # 60 entries, 180 lines
    out = ss.render_stdout(
        _groups(evolve=multi), resumed=False, overflow_file=tmp_path / "f.txt"
    )
    assert "evolve: 180 lines omitted" in out


def test_cheapest_group_degrades_first(tmp_path):
    out = ss.render_stdout(
        _groups(evolve=_big("evolve line", 120)),
        resumed=False,
        overflow_file=tmp_path / "f.txt",
    )
    meter = out.splitlines()[-1]
    assert "decay: 12 rows" in out
    assert "dropped: evolve" in meter and "decay" not in meter


def test_rank_not_output_order_decides_what_degrades(tmp_path):
    # `late` is printed second but is the cheapest to lose (rank 0); only it
    # needs to go. A degrade loop that followed output order would drop `early`.
    big = _big("line", 60)
    groups = [ss.MsgGroup("early", big, rank=5), ss.MsgGroup("late", list(big), rank=0)]
    out = ss.render_stdout(groups, resumed=False, overflow_file=tmp_path / "f.txt")
    meter = out.splitlines()[-1]
    assert "dropped: late" in meter and "early" not in meter
    assert len(out) <= ss.STDOUT_BUDGET


def test_a_pointer_longer_than_its_group_is_not_used(tmp_path):
    # Alerts alone are over budget; replacing a 1-line group with a longer
    # pointer would only cost space and mislabel it "dropped".
    alerts = _big("⚠️ drift", 200, width=120)
    out = ss.render_stdout(
        _groups(decay=["d"], evolve=["e"], hook_drift=alerts),
        resumed=False,
        overflow_file=tmp_path / "f.txt",
    )
    assert "dropped" not in out.splitlines()[-1]


def test_an_alert_group_is_never_degraded_even_when_oversized(tmp_path):
    alerts = _big("⚠️ drift", 60, width=60)  # ~4.5k, fits on its own
    out = ss.render_stdout(
        _groups(evolve=_big("evolve line", 120), hook_drift=alerts),
        resumed=False,
        overflow_file=tmp_path / "f.txt",
    )
    assert len(out) <= ss.STDOUT_BUDGET
    assert all(line in out for line in alerts)


def test_alerts_alone_over_budget_are_cut_on_a_line_boundary_and_say_so(tmp_path):
    alerts = _big("⚠️ drift", 200, width=120)  # ~26k
    full = tmp_path / "f.txt"
    out = ss.render_stdout(
        _groups(hook_drift=alerts), resumed=False, overflow_file=full
    )
    assert len(out) <= ss.STDOUT_BUDGET
    body = out.splitlines()
    assert all(line in alerts or not line.startswith("⚠️ drift") for line in body)
    assert "body truncated" in body[-1] and str(full) in out
    assert FOOTER_MARK in out
    assert alerts[-1] in full.read_text()


def test_one_huge_line_is_clipped_and_cannot_push_later_alerts_out(tmp_path):
    groups = [
        ss.MsgGroup("brain", ["⚠️ brain: " + "z" * 9_500]),
        ss.MsgGroup("hook_drift", ["⚠️ hook drift: settings.json"]),
    ]
    out = ss.render_stdout(groups, resumed=False, overflow_file=tmp_path / "f.txt")
    assert len(out) <= ss.STDOUT_BUDGET
    assert "⚠️ hook drift: settings.json" in out
    assert "…(clipped)" in out


def test_a_failed_overflow_write_still_prints_alerts_and_footer(tmp_path, capsys):
    unwritable = tmp_path / "a-directory"
    unwritable.mkdir()  # write_text on a directory raises IsADirectoryError
    out = ss.render_stdout(
        _groups(evolve=_big("evolve line", 200)),
        resumed=False,
        overflow_file=unwritable,
    )
    assert "⚠️ hook drift: settings.json" in out and FOOTER_MARK in out
    assert "overflow write failed" in out
    assert str(unwritable) not in out.split("Brief written")[0]
    assert "overflow write failed" in capsys.readouterr().err


def test_meter_reports_size_against_the_budget(tmp_path):
    out = ss.render_stdout(_groups(), resumed=True, overflow_file=tmp_path / "f.txt")
    assert f"/ {ss.STDOUT_BUDGET:,}" in out.splitlines()[-1]
    assert "Session resumed." in out


def test_main_wiring_alert_groups_never_degrade_and_rank_order_is_cheapest_first():
    names = [
        "sync",
        "decay",
        "evolve",
        "config",
        "dedup",
        "brain",
        "orphans",
        "hook_drift",
    ]
    groups = ss.build_groups(**{name: [name] for name in names})
    assert [g.name for g in groups] == names
    # Each group carries its own input: a positional mix-up in main() goes red.
    assert [g.lines for g in groups] == [[name] for name in names]
    assert {g.name for g in groups if g.rank is None} == {
        "config",
        "brain",
        "orphans",
        "hook_drift",
    }
    degradable = sorted((g for g in groups if g.rank is not None), key=lambda g: g.rank)
    assert [g.name for g in degradable] == ["evolve", "decay", "sync", "dedup"]


def test_meter_mentions_clipping_even_when_a_group_was_dropped_too(tmp_path):
    groups = [
        ss.MsgGroup("brain", ["⚠️ brain: " + "z" * 3_000] * 4),
        ss.MsgGroup("evolve", _big("evolve line", 120), rank=0),
    ]
    meter = ss.render_stdout(
        groups, resumed=False, overflow_file=tmp_path / "f.txt"
    ).splitlines()[-1]
    assert "lines clipped" in meter and "dropped: evolve" in meter


def test_main_passes_orphan_drift_output_as_the_orphans_group(monkeypatch, tmp_path):
    """build_groups is pinned above; this pins that main() feeds it the right
    variable, so a wrong-name slip in main() goes red."""
    monkeypatch.setattr(ss, "init_state", lambda: (ss.make_fresh_state(), False))
    monkeypatch.setattr(ss, "write_state", lambda state: None)
    monkeypatch.setattr(ss, "build_brief", lambda state, resumed, **k: "brief")
    monkeypatch.setattr(ss, "pending_section", lambda: "## Pending\nNone.\n")
    monkeypatch.setattr(ss, "BRIEF_FILE", tmp_path / "brief.md")
    for name in (
        "brain_sync", "memory_decay", "evolve_check",
    ):
        monkeypatch.setattr(ss, name, lambda state, _n=name: [_n])
    for name in (
        "config_sync_check", "live_hooks_branch", "config_untracked",
        "secret_scan_status", "canary_liveness", "dedup_warning",
        "brain_check", "orphan_drift", "hook_drift",
    ):
        monkeypatch.setattr(ss, name, lambda _n=name: [_n])
    captured = {}
    monkeypatch.setattr(ss, "render_stdout", lambda groups, *a: captured.setdefault("g", groups) and "")
    ss.main()
    by_name = {g.name: g.lines for g in captured["g"]}
    assert by_name["orphans"] == ["orphan_drift"]
    assert by_name["brain"] == ["brain_check"]
