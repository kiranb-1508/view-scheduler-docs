"""The gate: freshness, fail-closed, the rolling window, and the report.

Every case runs against a fake Workspace — no network, no warehouse, no Spark.
The clock is pinned with `now=` so the rolling window is deterministic.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from conftest import FakeWorkspace
from tests import fixtures as f
from view_scheduler.config import Config
from view_scheduler.gate import Dependency, GateResult, ViewResult, can_refresh

UTC = timezone.utc
NOW = f.at("2026-08-06", "12:00")          # noon UTC
CONFIG = Config(timezone="UTC")


def ago(hours):
    return NOW - timedelta(hours=hours)


def write(hours_ago, mode="Overwrite", rows="100", version=0):
    return f.raw_history_row(version, ago(hours_ago), "WRITE",
                             {"mode": mode}, {"numOutputRows": rows})


def optimize(hours_ago, version=0):
    return f.raw_history_row(version, ago(hours_ago), "OPTIMIZE", {},
                             {"numRemovedFiles": "3"})


def streaming_update(hours_ago, version=0):
    """A streaming table's scheduled refresh, as DESCRIBE HISTORY returns it."""
    return f.raw_history_row(version, ago(hours_ago), "STREAMING UPDATE",
                             {}, {"numOutputRows": "50"})


def run(views, ws, **kwargs):
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("quiet", True)
    return can_refresh(views, ws, CONFIG, **kwargs)


def dep(result, view=0, name=None):
    """The Dependency named `name`, or the first one, from one view's result."""
    deps = result.views[view].dependencies
    if name is None:
        return deps[0]
    return next(d for d in deps if name in d.name)


def view_of(names):
    return {"c.s.v": f.uc_view("c.s.v", names),
            **{n: f.uc_table(n) for n in names}}


# --- the happy path ----------------------------------------------------------

def test_all_fresh_is_ready():
    ws = FakeWorkspace(tables=view_of(["c.s.t1", "c.s.t2"]),
                       history={"c.s.t1": [write(8)],
                                "c.s.t2": [write(6, mode="Append")]})
    result = run("c.s.v", ws)
    assert isinstance(result, GateResult)
    assert result.ready is True
    assert result.views[0].ready is True
    assert [d.ok for d in result.views[0].dependencies] == [True, True]
    assert result.blockers() == ()


def test_zero_row_append_still_counts_as_a_refresh():
    ws = FakeWorkspace(tables=view_of(["c.s.t"]),
                       history={"c.s.t": [write(8, mode="Append", rows="0")]})
    assert run("c.s.v", ws).ready is True


def test_maintenance_only_history_is_stale():
    ws = FakeWorkspace(tables=view_of(["c.s.t"]),
                       history={"c.s.t": [optimize(1)]})
    result = run("c.s.v", ws)
    assert result.ready is False
    assert dep(result).detail == "no qualifying refresh in history"


def test_newest_qualifying_write_wins_not_the_newest_commit():
    # A fresh OPTIMIZE must not masquerade as a refresh, and the newest
    # qualifying write must win even when it is not the first row.
    stale = FakeWorkspace(tables=view_of(["c.s.t"]),
                          history={"c.s.t": [optimize(1), write(40, version=1)]})
    result = run("c.s.v", stale)
    assert result.ready is False
    assert dep(result).last_refresh == ago(40)

    fresh = FakeWorkspace(tables=view_of(["c.s.t"]),
                          history={"c.s.t": [write(40, version=1),
                                             write(2, mode="Merge", version=2)]})
    assert run("c.s.v", fresh).ready is True


# --- blocking ----------------------------------------------------------------

def test_one_stale_blocks_and_the_report_names_the_view():
    ws = FakeWorkspace(
        tables=view_of(["c.s.t_fresh", "c.s.t_old"]),
        history={"c.s.t_fresh": [write(8)], "c.s.t_old": [write(56)]})
    result = run("c.s.v", ws)
    assert result.ready is False
    assert result.blockers() == ("c.s.t_old",)
    assert result.views[0].blockers() == ("c.s.t_old",)
    assert "older than the 24h window" in dep(result, name="t_old").detail
    text = str(result)
    assert "BLOCKED" in text and "c.s.t_old" in text


def test_no_history_blocks():
    ws = FakeWorkspace(tables=view_of(["c.s.t"]), history={})
    result = run("c.s.v", ws)
    assert result.ready is False
    assert dep(result).detail == "no qualifying refresh in history"


def test_unreadable_history_blocks_fail_closed():
    ws = FakeWorkspace(tables=view_of(["c.s.t"]),
                       history_errors={"c.s.t": RuntimeError(
                           "User does not have SELECT on c.s.t")})
    result = run("c.s.v", ws)
    assert result.ready is False
    assert dep(result).detail == "history unreadable (permission)"


def test_foreign_dependency_blocks():
    ws = FakeWorkspace(
        tables={"c.s.v": f.uc_view("c.s.v", ["c.s.t", "c.s.ext"]),
                "c.s.t": f.uc_table("c.s.t"),
                "c.s.ext": f.uc_table("c.s.ext", table_type="FOREIGN")},
        history={"c.s.t": [write(8)]})
    result = run("c.s.v", ws)
    assert result.ready is False
    assert result.blockers() == ("c.s.ext",)
    assert "FOREIGN" in dep(result, name="ext").detail


def test_cycle_blocks_instead_of_passing():
    ws = FakeWorkspace(tables=dict(f.CYCLE), history={})
    result = run("c.s.v_a", ws)
    assert result.ready is False, "a cycle must not report READY"
    assert "circular dependency" in dep(result).detail


def test_root_view_not_found_blocks():
    ws = FakeWorkspace(tables={}, table_errors={
        "c.s.missing": KeyError("Table 'c.s.missing' does not exist")})
    result = run("c.s.missing", ws)
    assert result.ready is False
    assert result.blockers() == ("c.s.missing",)
    assert dep(result).detail == "not found"


# --- special object types ----------------------------------------------------

def test_streaming_table_is_checked_from_its_history_like_a_table():
    """A scheduled streaming table is stale between runs, so it is checked.

    The rig's own st_stream went 24 days without a commit (2026-09-13 to
    2026-10-07). The old "always fresh" rule called it fresh through all of
    them. Verified live: DESCRIBE HISTORY works on a streaming table, and its
    refreshes are STREAMING UPDATE commits.
    """
    fresh = FakeWorkspace(
        tables={"c.s.v": f.uc_view("c.s.v", ["c.s.st"]),
                "c.s.st": f.uc_table("c.s.st", table_type="STREAMING_TABLE")},
        history={"c.s.st": [streaming_update(5)]})
    result = run("c.s.v", fresh)
    assert result.ready is True
    assert dep(result).kind == "STREAMING_TABLE"
    assert dep(result).last_refresh == ago(5)
    assert "c.s.st" in fresh.sql_calls, "a streaming table's history is read"

    stale = FakeWorkspace(
        tables={"c.s.v": f.uc_view("c.s.v", ["c.s.st"]),
                "c.s.st": f.uc_table("c.s.st", table_type="STREAMING_TABLE")},
        history={"c.s.st": [streaming_update(30)]})
    result = run("c.s.v", stale)
    assert result.ready is False, "a scheduled streaming table goes stale between runs"
    assert "older than the 24h window" in dep(result).detail


def test_streaming_table_with_no_history_blocks():
    ws = FakeWorkspace(
        tables={"c.s.v": f.uc_view("c.s.v", ["c.s.st"]),
                "c.s.st": f.uc_table("c.s.st", table_type="STREAMING_TABLE")},
        history={})
    result = run("c.s.v", ws)
    assert result.ready is False
    assert "no qualifying refresh" in dep(result).detail


def test_streaming_table_whose_history_is_unreadable_blocks():
    ws = FakeWorkspace(
        tables={"c.s.v": f.uc_view("c.s.v", ["c.s.st"]),
                "c.s.st": f.uc_table("c.s.st", table_type="STREAMING_TABLE")},
        history_errors={"c.s.st": RuntimeError("User does not have permission")})
    result = run("c.s.v", ws)
    assert result.ready is False, "an unreadable streaming table fails closed"
    assert "history unreadable" in dep(result).detail


def test_materialized_view_reads_its_event_log():
    ws = FakeWorkspace(
        tables={"c.s.v": f.uc_view("c.s.v", ["c.s.mv"]),
                "c.s.mv": f.uc_table("c.s.mv", table_type="MATERIALIZED_VIEW")},
        mv_events={"c.s.mv": [{"ts_epoch": str(int(ago(5).timestamp()))}]},
        history_errors={"c.s.mv": RuntimeError(
            "[EXPECT_TABLE_NOT_VIEW.NO_ALTERNATIVE] expects a table")})
    result = run("c.s.v", ws)
    assert result.ready is True
    assert dep(result).last_refresh == ago(5)


def test_materialized_view_whose_event_log_fails_blocks():
    ws = FakeWorkspace(
        tables={"c.s.v": f.uc_view("c.s.v", ["c.s.mv"]),
                "c.s.mv": f.uc_table("c.s.mv", table_type="MATERIALIZED_VIEW")},
        history_errors={"c.s.mv": RuntimeError("EXPECT_TABLE_NOT_VIEW expects a table")},
        mv_event_errors={"c.s.mv": RuntimeError(
            "User does not have permission on the pipeline")})
    result = run("c.s.v", ws)
    assert result.ready is False
    assert "history unreadable" in dep(result).detail


# --- notes vs blockers -------------------------------------------------------

def test_function_dependency_is_a_note_not_a_blocker():
    ws = FakeWorkspace(tables={"c.s.v": f.uc_view("c.s.v", ["c.s.t"], functions=1),
                               "c.s.t": f.uc_table("c.s.t")},
                       history={"c.s.t": [write(8)]})
    result = run("c.s.v", ws)
    assert result.ready is True
    note = dep(result, name="udf")
    assert note.ok is True and "function" in note.detail


# --- the rolling window ------------------------------------------------------

def test_write_at_the_window_edge_is_fresh_just_past_is_stale():
    at_edge = FakeWorkspace(tables=view_of(["c.s.t"]),
                            history={"c.s.t": [write(24)]})
    assert run("c.s.v", at_edge).ready is True

    past = FakeWorkspace(tables=view_of(["c.s.t"]),
                         history={"c.s.t": [f.raw_history_row(
                             0, ago(24) - timedelta(seconds=1), "WRITE",
                             {"mode": "Overwrite"}, {})]})
    assert run("c.s.v", past).ready is False


def test_fresh_hours_can_be_overridden_per_call():
    ws = FakeWorkspace(tables=view_of(["c.s.t"]), history={"c.s.t": [write(42)]})
    assert run("c.s.v", ws).ready is False
    assert run("c.s.v", ws, fresh_hours=48).ready is True


def test_config_fresh_hours_is_used_when_not_overridden():
    ws = FakeWorkspace(tables=view_of(["c.s.t"]), history={"c.s.t": [write(42)]})
    wide = Config(timezone="UTC", fresh_hours=48)
    assert can_refresh("c.s.v", ws, wide, now=NOW, quiet=True).ready is True


def test_non_positive_window_is_rejected():
    ws = FakeWorkspace(tables={})
    with pytest.raises(ValueError, match="fresh_hours"):
        run("c.s.v", ws, fresh_hours=0)


def test_future_dated_write_is_ignored():
    ws = FakeWorkspace(tables=view_of(["c.s.t"]),
                       history={"c.s.t": [f.raw_history_row(
                           0, NOW + timedelta(hours=2), "WRITE",
                           {"mode": "Overwrite"}, {})]})
    result = run("c.s.v", ws)
    assert result.ready is False
    assert dep(result).detail == "no qualifying refresh in history"


def test_naive_now_is_localised_to_the_config_timezone():
    ws = FakeWorkspace(tables=view_of(["c.s.t"]), history={"c.s.t": [write(8)]})
    result = can_refresh("c.s.v", ws, CONFIG,
                         now=datetime(2026, 8, 6, 12, 0), quiet=True)
    assert result.ready is True


# --- lists, caching, printing ------------------------------------------------

def test_list_gives_per_view_detail_and_one_overall_flag():
    ws = FakeWorkspace(
        tables={"c.s.v1": f.uc_view("c.s.v1", ["c.s.t1"]),
                "c.s.t1": f.uc_table("c.s.t1"),
                "c.s.v2": f.uc_view("c.s.v2", ["c.s.t2"]),
                "c.s.t2": f.uc_table("c.s.t2")},
        history={"c.s.t1": [write(8)], "c.s.t2": [write(100)]})
    result = run(["c.s.v1", "c.s.v2"], ws)
    assert [v.view for v in result.views] == ["c.s.v1", "c.s.v2"]
    assert result.views[0].ready is True
    assert result.views[1].ready is False
    assert result.ready is False


def test_shared_dependency_is_read_once():
    ws = FakeWorkspace(
        tables={"c.s.v1": f.uc_view("c.s.v1", ["c.s.shared"]),
                "c.s.v2": f.uc_view("c.s.v2", ["c.s.shared"]),
                "c.s.shared": f.uc_table("c.s.shared")},
        history={"c.s.shared": [write(8)]})
    run(["c.s.v1", "c.s.v2"], ws)
    assert ws.get_table_calls.count("c.s.shared") == 1
    assert ws.sql_calls.count("c.s.shared") == 1


def test_a_failed_lookup_is_not_retried_for_every_view():
    ws = FakeWorkspace(
        tables={"c.s.v1": f.uc_view("c.s.v1", ["c.s.broken"]),
                "c.s.v2": f.uc_view("c.s.v2", ["c.s.broken"]),
                "c.s.broken": f.uc_table("c.s.broken")},
        table_errors={"c.s.broken": PermissionError("does not have USAGE")})
    result = run(["c.s.v1", "c.s.v2"], ws)
    assert ws.get_table_calls.count("c.s.broken") == 1
    assert result.ready is False


def test_duplicate_view_names_are_both_reported():
    ws = FakeWorkspace(tables=view_of(["c.s.t"]), history={"c.s.t": [write(8)]})
    result = run(["c.s.v", "c.s.v"], ws)
    assert len(result.views) == 2
    assert result.ready is True


def test_prints_by_default_and_is_silent_when_quiet(capsys):
    ws = FakeWorkspace(tables=view_of(["c.s.t"]), history={"c.s.t": [write(8)]})
    can_refresh("c.s.v", ws, CONFIG, now=NOW)
    assert "GATE  READY" in capsys.readouterr().out

    can_refresh("c.s.v", ws, CONFIG, now=NOW, quiet=True)
    assert capsys.readouterr().out == ""


def test_to_dict_is_json_serialisable():
    ws = FakeWorkspace(tables=view_of(["c.s.t"]), history={"c.s.t": [write(8)]})
    payload = run("c.s.v", ws).to_dict()
    assert payload["ready"] is True
    assert payload["blockers"] == []
    assert payload["views"][0]["dependencies"][0]["name"] == "c.s.t"
    json.dumps(payload)   # must not raise


def test_empty_input_is_rejected():
    with pytest.raises(ValueError, match="at least one view"):
        can_refresh([], FakeWorkspace(tables={}), CONFIG, now=NOW, quiet=True)


def test_default_workspace_requires_the_environment(monkeypatch):
    monkeypatch.delenv("WAREHOUSE_ID", raising=False)
    with pytest.raises(ValueError, match="WAREHOUSE_ID"):
        can_refresh("c.s.v", None, CONFIG, now=NOW, quiet=True)


def test_result_types_are_exported_and_frozen():
    ws = FakeWorkspace(tables=view_of(["c.s.t"]), history={"c.s.t": [write(8)]})
    result = run("c.s.v", ws)
    assert isinstance(result.views[0], ViewResult)
    assert isinstance(result.views[0].dependencies[0], Dependency)
    with pytest.raises(Exception):
        result.ready = False        # frozen


# --- the report --------------------------------------------------------------

def test_golden_report():
    ws = FakeWorkspace(
        tables=view_of(["c.s.t_fresh", "c.s.t_old"]),
        history={"c.s.t_fresh": [write(8)], "c.s.t_old": [write(56)]})
    expected = "\n".join([
        "GATE  BLOCKED   1 view, window 24h, times UTC",
        "",
        "  BLOCKED c.s.v",
        "      stale  c.s.t_old  (MANAGED)  last refresh 2026-08-04 04:00 UTC "
        "(2d 8h ago) — older than the 24h window",
        "      fresh  c.s.t_fresh  (MANAGED)  last refresh 2026-08-06 04:00 UTC "
        "(8h 00m ago)",
        "",
        "To unblock, refresh:",
        "  c.s.t_old   (needed by c.s.v)",
    ])
    assert run("c.s.v", ws).render() == expected
