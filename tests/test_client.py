"""The read-only I/O layer, exercised offline through a fake Workspace.

read_history and read_mv_refreshes were only ever reached against a live
warehouse; the materialized-view fallback in particular had no test at any
level, despite being the trickiest path (DESCRIBE HISTORY is rejected on an MV,
so refresh times come from the pipeline event log instead).
"""

from datetime import timezone

import pytest

from conftest import FakeWorkspace
from view_scheduler.client import (
    failure_category, parse_json_field, read_history, read_mv_refreshes,
)
from tests import fixtures as f

UTC = timezone.utc


# --- read_history ------------------------------------------------------------

def test_read_history_parses_the_raw_sql_api_shape():
    ws = FakeWorkspace(history={"c.s.t": f.OPERATION_ZOO})
    commits = read_history(ws, "c.s.t")
    assert len(commits) == len(f.OPERATION_ZOO)
    # operationParameters/operationMetrics arrive as JSON strings and must be
    # parsed back to maps (or None when absent).
    write = next(c for c in commits if c.operation == "WRITE" and c.version == 6)
    assert write.params["mode"] == "Append"
    assert write.metrics["numOutputRows"] == "1"
    update = next(c for c in commits if c.operation == "UPDATE")
    assert update.metrics is None


# --- read_mv_refreshes (the previously untested MV fallback) ------------------

def completed_projection(completions):
    """What the event_log SELECT returns after its COMPLETED filter: the noise
    rows from mv_event_rows dropped, projected to the single ts_epoch column."""
    from datetime import datetime, timezone
    return [{"ts_epoch": str(int(datetime.strptime(
                r["timestamp"], "%Y-%m-%dT%H:%M:%S.000Z")
                .replace(tzinfo=timezone.utc).timestamp()))}
            for r in f.mv_event_rows(completions)
            if r["event_type"] == "update_progress" and r["state"] == "COMPLETED"]


def test_read_mv_refreshes_turns_completed_events_into_commits():
    completions = [f.at("2026-09-01", "04:31"), f.at("2026-09-02", "04:33"),
                   f.at("2026-09-03", "04:30")]
    ws = FakeWorkspace(mv_events={"c.s.mv": completed_projection(completions)})
    commits = read_mv_refreshes(ws, "c.s.mv")
    assert len(commits) == 3
    assert all(c.operation == "MV REFRESH" for c in commits)
    assert [c.timestamp for c in commits] == completions


def test_read_mv_refreshes_on_an_mv_that_never_refreshed_is_empty_not_an_error():
    ws = FakeWorkspace(mv_events={"c.s.mv": []})
    assert read_mv_refreshes(ws, "c.s.mv") == []


# --- failure_category (shared detection used by both _reason helpers) ---------

@pytest.mark.parametrize("message,expected", [
    ("User does not have SELECT on c.s.t", "permission"),
    ("PERMISSION_DENIED: 403", "permission"),
    ("Table c.s.t does not exist", "not_found"),
    ("404 Not Found", "not_found"),
    ("[EXPECT_TABLE_NOT_VIEW.NO_ALTERNATIVE] expects a table", "mv_no_history"),
    ("some transient network blip", "other"),
])
def test_failure_category(message, expected):
    assert failure_category(RuntimeError(message)) == expected


def test_parse_json_field_handles_string_map_and_absent():
    assert parse_json_field('{"mode": "Append"}') == {"mode": "Append"}
    assert parse_json_field({"mode": "Append"}) == {"mode": "Append"}
    assert parse_json_field(None) is None
    assert parse_json_field("not json") is None
