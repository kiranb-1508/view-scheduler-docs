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
    failure_category, parse_json_field, read_commits, read_delta_history,
    read_history, read_mv_refreshes,
)
from tests import fixtures as f

UTC = timezone.utc


class RecordingWorkspace(FakeWorkspace):
    """A FakeWorkspace that also keeps every SQL statement it was given.

    `FakeWorkspace.sql_calls` stores only the target, so it cannot show the
    text. This subclass records the statement too, which the SQL-filter tests
    need.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.statements: list[str] = []

    def sql(self, statement, target):
        self.statements.append(statement)
        return super().sql(statement, target)


# --- history window filters --------------------------------------------------

def test_read_history_without_since_epoch_has_no_time_filter():
    ws = RecordingWorkspace()
    read_history(ws, "c.s.t")
    assert "WHERE unix_timestamp(timestamp) >=" not in ws.statements[-1]


def test_read_history_with_since_epoch_adds_the_time_filter():
    ws = RecordingWorkspace()
    read_history(ws, "c.s.t", since_epoch=1700000000)
    assert "WHERE unix_timestamp(timestamp) >= 1700000000" in ws.statements[-1]


def test_read_mv_refreshes_with_since_epoch_adds_the_time_filter():
    ws = RecordingWorkspace()
    read_mv_refreshes(ws, "c.s.mv", since_epoch=1700000000)
    assert "AND unix_timestamp(timestamp) >= 1700000000" in ws.statements[-1]


def test_read_commits_forwards_since_epoch_to_both_readers(monkeypatch):
    import view_scheduler.client as client
    ws = RecordingWorkspace()
    seen: list[tuple[str, int | None]] = []

    def fake_history(ws_, table, *, with_metrics=True, since_epoch=None):
        seen.append(("history", since_epoch))
        return []

    def fake_mv(ws_, mv, *, since_epoch=None):
        seen.append(("mv", since_epoch))
        return []

    monkeypatch.setattr(client, "read_delta_history", fake_history)
    monkeypatch.setattr(client, "read_mv_refreshes", fake_mv)

    read_commits(ws, "c.s.t", since_epoch=1700000000)
    assert seen == [("history", 1700000000)]

    # The MV fallback must forward the same window.
    def rejecting_history(ws_, table, *, with_metrics=True, since_epoch=None):
        raise RuntimeError("EXPECT_TABLE_NOT_VIEW")

    monkeypatch.setattr(client, "read_delta_history", rejecting_history)
    read_commits(ws, "c.s.mv", since_epoch=1700000000)
    assert seen[-1] == ("mv", 1700000000)


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
