"""The read-only I/O layer, exercised offline through a fake Workspace.

read_history and read_mv_refreshes were only ever reached against a live
warehouse; the materialized-view fallback in particular had no test at any
level, despite being the trickiest path (DESCRIBE HISTORY is rejected on an MV,
so refresh times come from the pipeline event log instead).
"""

import json
import logging
from datetime import timezone
from types import SimpleNamespace

import pytest

from conftest import FakeWorkspace
import view_scheduler.client as client
from view_scheduler.client import (
    Workspace, failure_category, parse_json_field, read_commits,
    read_delta_history, read_history, read_mv_refreshes,
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


# --- is_transient: the decision table ----------------------------------------

class FakeHttpError(Exception):
    """A stand-in for an SDK error that carries an HTTP status code."""

    def __init__(self, message, *, status_code=None, retry_after=None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class FakeSdkError(Exception):
    """Shaped like the real databricks-sdk `DatabricksError`.

    The real error carries `error_code` (a string) and `retry_after_secs` (an
    int). It carries no `status_code` and no `retry_after`.
    """

    def __init__(self, message, *, error_code=None, retry_after_secs=None):
        super().__init__(message)
        self.error_code = error_code
        self.retry_after_secs = retry_after_secs


@pytest.mark.parametrize("exc,expected", [
    # A transient HTTP status: worth a retry.
    (FakeHttpError("x", status_code=429), True),
    (FakeHttpError("x", status_code=500), True),
    (FakeHttpError("x", status_code=502), True),
    (FakeHttpError("x", status_code=503), True),
    (FakeHttpError("x", status_code=504), True),
    # A transient word, when the exception carries no status.
    (RuntimeError("rate limit reached"), True),
    (RuntimeError("service temporarily unavailable"), True),
    (RuntimeError("connection reset by peer"), True),
    (RuntimeError("the request timed out"), True),
    (RuntimeError("wait timeout"), True),
    (RuntimeError("the statement was canceled"), True),
    (RuntimeError("too many requests"), True),
    (RuntimeError("the service is currently unavailable"), True),
    # The real SDK error carries `error_code`, not `status_code`.
    (FakeSdkError("too many requests", error_code="RESOURCE_EXHAUSTED"), True),
    (FakeSdkError("quota exceeded for this workspace",
                  error_code="REQUEST_LIMIT_EXCEEDED"), True),
    (FakeSdkError("the service is currently unavailable",
                  error_code="UNAVAILABLE"), True),
    (FakeSdkError("permission denied", error_code="PERMISSION_DENIED"), False),
    (FakeSdkError("unexpected failure",
                  error_code="INVALID_PARAMETER_VALUE"), False),
    # A permanent failure: fail closed, at once.
    (FakeHttpError("x", status_code=401), False),
    (FakeHttpError("x", status_code=403), False),
    (FakeHttpError("x", status_code=404), False),
    (RuntimeError("User does not have SELECT on c.s.t"), False),
    (RuntimeError("PERMISSION_DENIED"), False),
    (RuntimeError("Table c.s.t does not exist"), False),
    (RuntimeError("404 Not Found"), False),
    (RuntimeError("[EXPECT_TABLE_NOT_VIEW.NO_ALTERNATIVE] expects a table"),
     False),
    (RuntimeError("PARSE_SYNTAX_ERROR: syntax error at or near 'FROM'"), False),
    # The status code wins over the text, in both directions.
    (FakeHttpError("connection reset", status_code=403), False),
    (FakeHttpError("permission denied", status_code=503), True),
])
def test_is_transient_follows_the_decision_table(exc, expected):
    assert client.is_transient(exc) is expected


def test_status_code_still_wins_over_error_code():
    """The status-first check stays first, for exception types that carry one."""
    exc = FakeSdkError("connection reset", error_code="RESOURCE_EXHAUSTED")
    exc.status_code = 403
    assert client.is_transient(exc) is False


# --- with_retry: three attempts, fail closed at once --------------------------

class SleepRecorder:
    """A fake sleep. It records each wait instead of waiting."""

    def __init__(self):
        self.waits: list[float] = []

    def __call__(self, seconds):
        self.waits.append(seconds)


class FlakyApi:
    """A fake api_client. It fails the staged times, then answers."""

    def __init__(self, failures, result):
        self._failures = list(failures)
        self._result = result
        self.calls = 0

    def do(self, method, path):
        self.calls += 1
        if self._failures:
            raise self._failures.pop(0)
        return self._result


class FakeSdk:
    """The WorkspaceClient surface the readers touch."""

    def __init__(self, api_client=None, statement_execution=None):
        self.api_client = api_client
        self.statement_execution = statement_execution


def workspace_with(api_client=None, statement_execution=None):
    return Workspace(warehouse_id="wh",
                     client=FakeSdk(api_client, statement_execution))


def test_a_transient_error_is_retried_and_then_succeeds(monkeypatch):
    api = FlakyApi([FakeHttpError("boom", status_code=503),
                    FakeHttpError("boom", status_code=503)],
                   {"name": "c.s.t"})
    ws = workspace_with(api_client=api)
    recorder = SleepRecorder()
    monkeypatch.setattr(client, "_sleep", recorder)

    assert ws.get_table("c.s.t") == {"name": "c.s.t"}
    assert recorder.waits == [1.0, 2.0]
    assert api.calls == 3


def test_a_permanent_error_is_not_retried(monkeypatch):
    api = FlakyApi([FakeHttpError("no access", status_code=403)],
                   {"name": "c.s.t"})
    ws = workspace_with(api_client=api)
    recorder = SleepRecorder()
    monkeypatch.setattr(client, "_sleep", recorder)

    with pytest.raises(FakeHttpError):
        ws.get_table("c.s.t")
    assert recorder.waits == []
    assert api.calls == 1


def test_all_attempts_exhausted_reraises_the_last_error(monkeypatch):
    api = FlakyApi([FakeHttpError("boom", status_code=503) for _ in range(3)],
                   {"name": "c.s.t"})
    ws = workspace_with(api_client=api)
    recorder = SleepRecorder()
    monkeypatch.setattr(client, "_sleep", recorder)

    with pytest.raises(FakeHttpError):
        ws.get_table("c.s.t")
    assert recorder.waits == [1.0, 2.0]
    assert api.calls == 3


def test_a_longer_retry_after_value_wins(monkeypatch):
    api = FlakyApi([FakeHttpError("slow down", status_code=429,
                                  retry_after=5.0)], {"name": "c.s.t"})
    ws = workspace_with(api_client=api)
    recorder = SleepRecorder()
    monkeypatch.setattr(client, "_sleep", recorder)

    assert ws.get_table("c.s.t") == {"name": "c.s.t"}
    assert recorder.waits == [5.0]


def test_a_real_sdk_retry_after_secs_value_wins(monkeypatch):
    """The real SDK error names the floor `retry_after_secs`."""
    api = FlakyApi([FakeSdkError("too many requests",
                                 error_code="RESOURCE_EXHAUSTED",
                                 retry_after_secs=5)], {"name": "c.s.t"})
    ws = workspace_with(api_client=api)
    recorder = SleepRecorder()
    monkeypatch.setattr(client, "_sleep", recorder)

    assert ws.get_table("c.s.t") == {"name": "c.s.t"}
    assert recorder.waits == [5.0]


def test_the_computed_wait_remains_the_floor(monkeypatch):
    api = FlakyApi([FakeSdkError("too many requests",
                                 error_code="RESOURCE_EXHAUSTED",
                                 retry_after_secs=0)], {"name": "c.s.t"})
    ws = workspace_with(api_client=api)
    recorder = SleepRecorder()
    monkeypatch.setattr(client, "_sleep", recorder)

    assert ws.get_table("c.s.t") == {"name": "c.s.t"}
    assert recorder.waits == [1.0]


def test_a_real_shape_sdk_429_is_retried(monkeypatch):
    """A real SDK 429 carries `error_code`, not `status_code`, so the word
    list alone must still see it, and a known code must retry on its own."""
    api = FlakyApi([
        FakeSdkError("Too many requests", error_code="RESOURCE_EXHAUSTED"),
        FakeSdkError("quota exceeded", error_code="REQUEST_LIMIT_EXCEEDED"),
    ], {"name": "c.s.t"})
    ws = workspace_with(api_client=api)
    recorder = SleepRecorder()
    monkeypatch.setattr(client, "_sleep", recorder)

    assert ws.get_table("c.s.t") == {"name": "c.s.t"}
    assert recorder.waits == [1.0, 2.0]
    assert api.calls == 3


def test_with_retry_takes_an_injected_sleep():
    calls = []
    recorder = SleepRecorder()

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise FakeHttpError("boom", status_code=503)
        return "done"

    assert client.with_retry(flaky, kind="probe", target="c.s.t",
                             sleep=recorder) == "done"
    assert recorder.waits == [1.0, 2.0]


def test_the_spark_path_is_not_retried():
    class FailingSpark:
        def sql(self, statement):
            raise FakeHttpError("boom", status_code=503)

    ws = Workspace(spark=FailingSpark())
    with pytest.raises(FakeHttpError):
        ws.sql("SELECT 1", target="probe")


class FakeStatus:
    def __init__(self, state):
        self.state = SimpleNamespace(value=state)
        self.error = None


class FakeManifest:
    def __init__(self, columns):
        self.truncated = False
        self.schema = SimpleNamespace(
            columns=[SimpleNamespace(name=name) for name in columns])


class FakeResult:
    def __init__(self, data, next_chunk_index=None):
        self.data_array = data
        self.next_chunk_index = next_chunk_index


class FakeStatementResponse:
    def __init__(self, columns, data, next_chunk_index=None,
                 state="SUCCEEDED"):
        self.statement_id = "stmt-1"
        self.status = FakeStatus(state)
        self.manifest = FakeManifest(columns)
        self.result = FakeResult(data, next_chunk_index)


class ChunkFailingStatements:
    """The first page arrives; the first chunk fetch fails once."""

    def __init__(self, failures):
        self._failures = list(failures)
        self.chunk_calls = 0

    def execute_statement(self, **kwargs):
        return FakeStatementResponse(columns=["version"], data=[["1"]],
                                     next_chunk_index=1)

    def get_statement_result_chunk_n(self, statement_id, chunk):
        self.chunk_calls += 1
        if self._failures:
            raise self._failures.pop(0)
        return SimpleNamespace(data_array=[["2"]], next_chunk_index=None)


def test_a_transient_chunk_fetch_is_retried(monkeypatch):
    statements = ChunkFailingStatements(
        [FakeHttpError("boom", status_code=503)])
    ws = workspace_with(statement_execution=statements)
    recorder = SleepRecorder()
    monkeypatch.setattr(client, "_sleep", recorder)

    rows = ws.sql("SELECT version FROM c.s.t", target="c.s.t")
    assert [row["version"] for row in rows] == ["1", "2"]
    assert recorder.waits == [1.0]
    assert statements.chunk_calls == 2


class StateStatements:
    """Every submission returns the same non-SUCCEEDED statement state."""

    def __init__(self, state):
        self.state = state
        self.calls = 0

    def execute_statement(self, **kwargs):
        self.calls += 1
        return FakeStatementResponse(columns=["version"], data=[],
                                     state=self.state)

    def get_statement_result_chunk_n(self, statement_id, chunk):
        return SimpleNamespace(data_array=[], next_chunk_index=None)


def test_a_canceled_statement_state_is_retried_twice_then_raises(monkeypatch):
    """A CANCEL from the wait timeout is a passing failure. The state check
    sits inside the retried call, so it gets three attempts in total."""
    statements = StateStatements("CANCELED")
    ws = workspace_with(statement_execution=statements)
    recorder = SleepRecorder()
    monkeypatch.setattr(client, "_sleep", recorder)

    with pytest.raises(RuntimeError, match="CANCELED"):
        ws.sql("SELECT version FROM c.s.t", target="c.s.t")
    assert statements.calls == 3
    assert recorder.waits == [1.0, 2.0]


class MvRejectingStatements:
    """DESCRIBE HISTORY raises the MV rejection; event_log answers."""

    def __init__(self):
        self.statements: list[str] = []

    def execute_statement(self, **kwargs):
        self.statements.append(kwargs["statement"])
        if "DESCRIBE HISTORY" in kwargs["statement"]:
            raise RuntimeError(
                "[EXPECT_TABLE_NOT_VIEW.NO_ALTERNATIVE] expects a table")
        return FakeStatementResponse(columns=["ts_epoch"], data=[])

    def get_statement_result_chunk_n(self, statement_id, chunk):
        return SimpleNamespace(data_array=[], next_chunk_index=None)


def test_the_mv_rejection_is_not_retried(monkeypatch):
    statements = MvRejectingStatements()
    ws = workspace_with(statement_execution=statements)
    recorder = SleepRecorder()
    monkeypatch.setattr(client, "_sleep", recorder)

    assert read_commits(ws, "c.s.mv") == []
    assert recorder.waits == []
    # The rejection must fall through on the first attempt: one DESCRIBE, then
    # one event_log read. A retry would submit DESCRIBE three times.
    assert len(statements.statements) == 2
    assert "DESCRIBE HISTORY" in statements.statements[0]
    assert "event_log" in statements.statements[1]


def test_every_retry_logs_one_line(monkeypatch, caplog):
    api = FlakyApi([FakeHttpError("boom", status_code=503),
                    FakeHttpError("boom", status_code=503)],
                   {"name": "c.s.t"})
    ws = workspace_with(api_client=api)
    recorder = SleepRecorder()
    monkeypatch.setattr(client, "_sleep", recorder)

    with caplog.at_level(logging.INFO, logger="view_scheduler"):
        ws.get_table("c.s.t")

    lines = [json.loads(record.getMessage()) for record in caplog.records
             if "attempt" in record.getMessage()]
    assert [line["attempt"] for line in lines] == [1, 2]
    assert [line["wait_s"] for line in lines] == [1.0, 2.0]
    assert all(line["call"] == "uc.tables.get" for line in lines)
    assert all(line["target"] == "c.s.t" for line in lines)
    assert all(line["error"] for line in lines)
