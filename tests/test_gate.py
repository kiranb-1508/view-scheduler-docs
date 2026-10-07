"""The gate: freshness, fail-closed, and the rolling window.

Every case runs against a fake Workspace — no network, no warehouse, no Spark.
The clock is pinned with `now=` so the rolling window is deterministic.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from conftest import FakeWorkspace
from tests import fixtures as f
from view_scheduler.config import Config
from view_scheduler.gate import GateResult, can_refresh

UTC = timezone.utc
NOW = f.at("2026-08-06", "12:00")          # noon UTC
CONFIG = Config(timezone="UTC")


def overwrite(day, hhmm, rows="100"):
    """One full-replace commit at a wall-clock time."""
    return [f.raw_history_row(0, f.at(day, hhmm), "WRITE",
                              {"mode": "Overwrite"}, {"numOutputRows": rows})]


def append(day, hhmm, rows="10"):
    return [f.raw_history_row(0, f.at(day, hhmm), "WRITE",
                              {"mode": "Append"}, {"numOutputRows": rows})]


def workspace(tables, history=None, **kwargs):
    return FakeWorkspace(tables=tables, history=history or {}, **kwargs)


# --- the happy path ----------------------------------------------------------

def test_all_fresh_is_ready():
    ws = workspace(
        tables={"c.s.v": f.uc_view("c.s.v", ["c.s.t1", "c.s.t2"]),
                "c.s.t1": f.uc_table("c.s.t1"),
                "c.s.t2": f.uc_table("c.s.t2")},
        history={"c.s.t1": overwrite("2026-08-06", "04:00"),
                 "c.s.t2": append("2026-08-06", "06:00")},
    )
    result = can_refresh("c.s.v", ws, CONFIG, now=NOW, quiet=True)
    assert result.ready is True
    assert result.views[0].ready is True
    assert len(result.views[0].dependencies) == 2
    assert all(dep.fresh for dep in result.views[0].dependencies)
    assert result.blockers() == []
    assert result.views[0].blockers == ()


def test_zero_row_append_still_counts_as_a_refresh():
    ws = workspace(
        tables={"c.s.v": f.uc_view("c.s.v", ["c.s.t"]), "c.s.t": f.uc_table("c.s.t")},
        history={"c.s.t": append("2026-08-06", "04:00", rows="0")},
    )
    assert can_refresh("c.s.v", ws, CONFIG, now=NOW, quiet=True).ready is True


def test_maintenance_only_history_is_not_a_refresh():
    ws = workspace(
        tables={"c.s.v": f.uc_view("c.s.v", ["c.s.t"]), "c.s.t": f.uc_table("c.s.t")},
        history={"c.s.t": [f.raw_history_row(
            0, f.at("2026-08-06", "04:00"), "OPTIMIZE", {}, {"numRemovedFiles": "3"})]},
    )
    result = can_refresh("c.s.v", ws, CONFIG, now=NOW, quiet=True)
    assert result.ready is False
    dep = result.views[0].dependencies[0]
    assert dep.last_refresh is None
    assert dep.reason == "no qualifying refresh in history"


# --- blocking ----------------------------------------------------------------

def test_one_stale_blocks_and_names_the_view():
    ws = workspace(
        tables={"c.s.v": f.uc_view("c.s.v", ["c.s.t_fresh", "c.s.t_old"]),
                "c.s.t_fresh": f.uc_table("c.s.t_fresh"),
                "c.s.t_old": f.uc_table("c.s.t_old")},
        history={"c.s.t_fresh": overwrite("2026-08-06", "05:00"),
                 "c.s.t_old": overwrite("2026-08-04", "05:00")},   # 55h old
    )
    result = can_refresh("c.s.v", ws, CONFIG, now=NOW, quiet=True)
    assert result.ready is False
    assert result.blockers() == [("c.s.v", "c.s.t_old")]
    text = str(result)
    assert "BLOCKED" in text
    assert "c.s.v" in text and "c.s.t_old" in text
    assert "outside the 24h window" in text


def test_no_history_blocks():
    ws = workspace(
        tables={"c.s.v": f.uc_view("c.s.v", ["c.s.t"]), "c.s.t": f.uc_table("c.s.t")},
        history={},
    )
    result = can_refresh("c.s.v", ws, CONFIG, now=NOW, quiet=True)
    assert result.ready is False
    assert result.views[0].dependencies[0].reason == "no qualifying refresh in history"


def test_unreadable_history_blocks_fail_closed():
    ws = workspace(
        tables={"c.s.v": f.uc_view("c.s.v", ["c.s.t"]), "c.s.t": f.uc_table("c.s.t")},
        history_errors={"c.s.t": RuntimeError("User does not have SELECT on c.s.t")},
    )
    result = can_refresh("c.s.v", ws, CONFIG, now=NOW, quiet=True)
    assert result.ready is False
    assert "history unreadable" in result.views[0].dependencies[0].reason


def test_foreign_dependency_blocks():
    ws = workspace(
        tables={"c.s.v": f.uc_view("c.s.v", ["c.s.t", "c.s.ext"]),
                "c.s.t": f.uc_table("c.s.t"),
                "c.s.ext": f.uc_table("c.s.ext", table_type="FOREIGN")},
        history={"c.s.t": overwrite("2026-08-06", "04:00")},
    )
    result = can_refresh("c.s.v", ws, CONFIG, now=NOW, quiet=True)
    assert result.ready is False
    assert result.blockers() == [("c.s.v", "c.s.ext")]


# --- special object types ----------------------------------------------------

def test_streaming_table_is_always_fresh():
    ws = workspace(
        tables={"c.s.v": f.uc_view("c.s.v", ["c.s.st"]),
                "c.s.st": f.uc_table("c.s.st", table_type="STREAMING_TABLE")},
        history={},   # deliberately no history at all
    )
    result = can_refresh("c.s.v", ws, CONFIG, now=NOW, quiet=True)
    assert result.ready is True
    dep = result.views[0].dependencies[0]
    assert dep.kind == "STREAMING_TABLE" and dep.fresh is True


def test_materialized_view_reads_its_event_log():
    ws = workspace(
        tables={"c.s.v": f.uc_view("c.s.v", ["c.s.mv"]),
                "c.s.mv": f.uc_table("c.s.mv", table_type="MATERIALIZED_VIEW")},
        mv_events={"c.s.mv": [
            {"ts_epoch": str(int(f.at("2026-08-06", "05:00").timestamp()))}]},
        history_errors={"c.s.mv": RuntimeError(
            "[EXPECT_TABLE_NOT_VIEW.NO_ALTERNATIVE] expects a table")},
    )
    result = can_refresh("c.s.v", ws, CONFIG, now=NOW, quiet=True)
    assert result.ready is True
    assert result.views[0].dependencies[0].last_refresh == f.at("2026-08-06", "05:00")


# --- notes vs blockers -------------------------------------------------------

def test_function_dependency_is_a_note_not_a_blocker():
    ws = workspace(
        tables={"c.s.v": f.uc_view("c.s.v", ["c.s.t"], functions=1),
                "c.s.t": f.uc_table("c.s.t")},
        history={"c.s.t": overwrite("2026-08-06", "04:00")},
    )
    result = can_refresh("c.s.v", ws, CONFIG, now=NOW, quiet=True)
    assert result.ready is True
    assert result.views[0].blockers == ()
    assert len(result.views[0].notes) == 1
    assert "function" in result.views[0].notes[0].reason


# --- the rolling window ------------------------------------------------------

def test_write_exactly_at_the_window_edge_is_fresh():
    ws = workspace(
        tables={"c.s.v": f.uc_view("c.s.v", ["c.s.t"]), "c.s.t": f.uc_table("c.s.t")},
        history={"c.s.t": [f.raw_history_row(
            0, NOW - timedelta(hours=24), "WRITE", {"mode": "Overwrite"}, {})]},
    )
    assert can_refresh("c.s.v", ws, CONFIG, now=NOW, quiet=True).ready is True


def test_write_just_past_the_window_edge_is_stale():
    ws = workspace(
        tables={"c.s.v": f.uc_view("c.s.v", ["c.s.t"]), "c.s.t": f.uc_table("c.s.t")},
        history={"c.s.t": [f.raw_history_row(
            0, NOW - timedelta(hours=24, seconds=1), "WRITE",
            {"mode": "Overwrite"}, {})]},
    )
    assert can_refresh("c.s.v", ws, CONFIG, now=NOW, quiet=True).ready is False


def test_fresh_hours_can_be_overridden_per_call():
    ws = workspace(
        tables={"c.s.v": f.uc_view("c.s.v", ["c.s.t"]), "c.s.t": f.uc_table("c.s.t")},
        history={"c.s.t": overwrite("2026-08-04", "18:00")},   # 42h old
    )
    assert can_refresh("c.s.v", ws, CONFIG, now=NOW, quiet=True).ready is False
    assert can_refresh("c.s.v", ws, CONFIG, now=NOW,
                       fresh_hours=48, quiet=True).ready is True


# --- lists, caching, printing ------------------------------------------------

def test_list_returns_per_view_detail_and_one_overall_flag():
    ws = workspace(
        tables={"c.s.v1": f.uc_view("c.s.v1", ["c.s.t1"]),
                "c.s.t1": f.uc_table("c.s.t1"),
                "c.s.v2": f.uc_view("c.s.v2", ["c.s.t2"]),
                "c.s.t2": f.uc_table("c.s.t2")},
        history={"c.s.t1": overwrite("2026-08-06", "04:00"),
                 "c.s.t2": overwrite("2026-08-01", "04:00")},   # stale
    )
    result = can_refresh(["c.s.v1", "c.s.v2"], ws, CONFIG, now=NOW, quiet=True)
    assert [v.view for v in result.views] == ["c.s.v1", "c.s.v2"]
    assert result.views[0].ready is True
    assert result.views[1].ready is False
    assert result.ready is False


def test_shared_dependency_is_fetched_once_across_a_list():
    class Counting(FakeWorkspace):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.looked_up = []

        def get_table(self, full_name):
            self.looked_up.append(full_name)
            return super().get_table(full_name)

    ws = Counting(
        tables={"c.s.v1": f.uc_view("c.s.v1", ["c.s.shared"]),
                "c.s.v2": f.uc_view("c.s.v2", ["c.s.shared"]),
                "c.s.shared": f.uc_table("c.s.shared")},
        history={"c.s.shared": overwrite("2026-08-06", "04:00")},
    )
    can_refresh(["c.s.v1", "c.s.v2"], ws, CONFIG, now=NOW, quiet=True)
    assert ws.looked_up.count("c.s.shared") == 1


def test_prints_by_default_and_is_silent_when_quiet(capsys):
    ws = workspace(
        tables={"c.s.v": f.uc_view("c.s.v", ["c.s.t"]), "c.s.t": f.uc_table("c.s.t")},
        history={"c.s.t": overwrite("2026-08-06", "04:00")},
    )
    can_refresh("c.s.v", ws, CONFIG, now=NOW)
    assert "GATE RESULT  READY" in capsys.readouterr().out

    can_refresh("c.s.v", ws, CONFIG, now=NOW, quiet=True)
    assert capsys.readouterr().out == ""


def test_to_dict_is_json_serialisable():
    ws = workspace(
        tables={"c.s.v": f.uc_view("c.s.v", ["c.s.t"]), "c.s.t": f.uc_table("c.s.t")},
        history={"c.s.t": overwrite("2026-08-06", "04:00")},
    )
    payload = can_refresh("c.s.v", ws, CONFIG, now=NOW, quiet=True).to_dict()
    assert payload["ready"] is True
    assert payload["views"][0]["dependencies"][0]["name"] == "c.s.t"
    json.dumps(payload)   # must not raise


def test_empty_input_is_rejected():
    ws = workspace(tables={})
    with pytest.raises(ValueError):
        can_refresh([], ws, CONFIG, now=NOW, quiet=True)


def test_result_is_a_gate_result():
    ws = workspace(
        tables={"c.s.v": f.uc_view("c.s.v", ["c.s.t"]), "c.s.t": f.uc_table("c.s.t")},
        history={"c.s.t": overwrite("2026-08-06", "04:00")},
    )
    assert isinstance(can_refresh("c.s.v", ws, CONFIG, now=NOW, quiet=True), GateResult)
