"""End-to-end orchestration through a fake Workspace.

run.analyze and its _read / _reason helpers were only reachable against a live
warehouse. This drives the whole path — walk, per-table history read, the
materialized-view event-log fallback, and the skipped-dependency wording —
with no network.
"""

from datetime import date, timezone

from conftest import FakeWorkspace
from view_scheduler.config import Config
from view_scheduler.run import _reason, analyze
from tests import fixtures as f

UTC = timezone.utc
CONFIG = Config()
TODAY = date(2026, 8, 6)   # the fixtures write on 2026-08-06


def uc_view(name, deps):
    return f.uc_view(name, deps)


def daily_overwrite(day, hhmm):
    return [f.raw_history_row(0, f.at(day, hhmm), "WRITE", {"mode": "Overwrite"},
                              {"numOutputRows": "100"})]


def test_analyze_reads_a_managed_table_and_a_materialized_view_dependency():
    """The view gates on one managed table (DESCRIBE HISTORY) and one MV
    (which rejects DESCRIBE HISTORY and must fall back to the event log)."""
    tables = {
        "c.s.v": f.uc_view("c.s.v", ["c.s.t", "c.s.mv"]),
        "c.s.t": f.uc_table("c.s.t"),
        "c.s.mv": f.uc_table("c.s.mv", table_type="MATERIALIZED_VIEW"),
    }
    ws = FakeWorkspace(
        tables=tables,
        history={"c.s.t": daily_overwrite("2026-08-06", "04:00")},
        mv_events={"c.s.mv": [
            {"ts_epoch": str(int(f.at("2026-08-06", "04:30").timestamp()))}]},
        # An MV rejects DESCRIBE HISTORY; _read must catch this and fall back.
        history_errors={"c.s.mv": RuntimeError(
            "[EXPECT_TABLE_NOT_VIEW.NO_ALTERNATIVE] expects a table")},
    )
    text, histories, skipped = analyze("c.s.v", ws, CONFIG, today=TODAY)
    names = {h.name for h in histories}
    assert names == {"c.s.t", "c.s.mv"}, "the MV fallback must yield history"
    assert skipped == []
    assert all(h.finish_times for h in histories)


def test_analyze_skips_a_dependency_whose_history_is_forbidden():
    tables = {
        "c.s.v": f.uc_view("c.s.v", ["c.s.t", "c.s.secret"]),
        "c.s.t": f.uc_table("c.s.t"),
        "c.s.secret": f.uc_table("c.s.secret"),
    }
    ws = FakeWorkspace(
        tables=tables,
        history={"c.s.t": daily_overwrite("2026-08-06", "04:00")},
        history_errors={"c.s.secret": RuntimeError(
            "User does not have SELECT on c.s.secret")},
    )
    text, histories, skipped = analyze("c.s.v", ws, CONFIG, today=TODAY)
    assert {h.name for h in histories} == {"c.s.t"}
    assert [(s.name, s.reason) for s in skipped] == [
        ("c.s.secret", "permission denied — cannot read its history")]


def test_analyze_carries_the_walkers_skips_through_to_the_report():
    """A FOREIGN dependency is skipped by the walker, never reaches _read, and
    still appears in the returned skipped list."""
    tables = {
        "c.s.v": f.uc_view("c.s.v", ["c.s.t", "c.s.ext"]),
        "c.s.t": f.uc_table("c.s.t"),
        "c.s.ext": f.uc_table("c.s.ext", table_type="FOREIGN"),
    }
    ws = FakeWorkspace(tables=tables,
                       history={"c.s.t": daily_overwrite("2026-08-06", "04:00")})
    _, histories, skipped = analyze("c.s.v", ws, CONFIG, today=TODAY)
    assert {h.name for h in histories} == {"c.s.t"}
    assert [s.name for s in skipped] == ["c.s.ext"]


def test_reason_wording():
    assert _reason(RuntimeError("does not have permission")) == \
        "permission denied — cannot read its history"
    assert _reason(RuntimeError("EXPECT_TABLE_NOT_VIEW")) == \
        "materialized view whose pipeline event log is not readable"
    assert _reason(RuntimeError("kaboom")).startswith("history unreadable")
