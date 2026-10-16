"""I/O layer. Read-only: metadata lookups, DESCRIBE HISTORY, event log queries."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any
from urllib.parse import quote

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import ExecuteStatementRequestOnWaitTimeout

from . import obs
from .history import qualifying


def active_spark() -> Any | None:
    """The live SparkSession inside this Python process, or None.

    Inside a Databricks notebook or job, a session is already running, and
    `spark.sql()` costs no warehouse. Import lazily: a laptop CLI has no
    pyspark, and that must stay an ordinary None, not an error.
    """
    try:
        from pyspark.sql import SparkSession
    except ImportError:
        return None
    return SparkSession.getActiveSession()


def quote_name(full_name: str) -> str:
    """Backtick each part of a dotted Unity Catalog name.

    A hyphen, a space, or a reserved word is a syntax error unless the name is
    quoted, and an unquoted name is an injection surface. An internal backtick
    is doubled so it cannot close the identifier early.
    """
    parts = [part.strip() for part in str(full_name).split(".")]
    if not parts or any(not part for part in parts):
        raise ValueError(f"not a Unity Catalog name: {full_name!r}")
    return ".".join("`" + part.replace("`", "``") + "`" for part in parts)


class Workspace:
    """The one I/O seam. Read-only: metadata through the SDK, history SQL
    through the warehouse — or through the SparkSession the code already
    runs inside.

    Resolution order for the history engine:

      1. an explicit `warehouse_id`,
      2. the $WAREHOUSE_ID environment variable,
      3. a live SparkSession (a Databricks notebook, job, or pipeline),
      4. an error that names every missing option.

    Metadata (`get_table`) always goes through the SDK client, which on
    Databricks compute needs no warehouse: it authenticates from the notebook
    context. The SDK client is built lazily, on first use.
    """

    #: None means "not built yet"; `w` builds the SDK client on first use,
    #: so a plain Workspace() with an active Spark session never touches it
    #: until get_table needs it. Define nothing here; the property below owns it.

    def __init__(self, warehouse_id: str | None = None,
                 client: WorkspaceClient | None = None,
                 spark: Any | None = None):
        self._w = client
        self.warehouse_id = warehouse_id
        self.spark = spark
        if warehouse_id is None and spark is None:
            warehouse_id = os.environ.get("WAREHOUSE_ID")
        if warehouse_id is None and spark is None:
            spark = active_spark()
        if warehouse_id is None and spark is None:
            raise ValueError(
                "no engine: pass Workspace(warehouse_id='...'), "
                "set the WAREHOUSE_ID environment variable, "
                "or run inside Databricks, where a Spark session exists")
        self.warehouse_id = warehouse_id
        self.spark = spark

    @property
    def w(self) -> WorkspaceClient:
        """The SDK client, built once, the first time it is needed."""
        if self._w is None:
            self._w = WorkspaceClient()
        return self._w

    # --- Unity Catalog -------------------------------------------------------

    def get_table(self, full_name: str) -> dict:
        """Raw GET for one table's metadata, including view_dependencies.

        The GET is retried on a passing failure and fails closed on a lasting
        one.
        """
        path = quote(full_name, safe=".")
        url = f"/api/2.1/unity-catalog/tables/{path}"
        with obs.call("uc.tables.get", full_name):
            return with_retry(lambda: self.w.api_client.do("GET", url),
                              kind="uc.tables.get", target=full_name)

    # --- SQL -----------------------------------------------------------------

    def sql(self, statement: str, target: str) -> list[dict[str, Any]]:
        """Run a read-only statement. Return every row as a dict of strings.

        Two engines, one seam. A SparkSession runs the statement on the local
        compute; otherwise the Statement API runs it on a warehouse. The rows
        must be interchangeable: same columns, same order, same facts.

        The statement path also follows result chunking to the end and refuses
        a truncated result, so a caller never silently sees only the first
        page of a large result. Each warehouse call — the submission and every
        chunk fetch — is retried on a passing failure. The Spark path is not
        retried: it is local compute, so a failure there is real.
        """
        if self.spark is not None:
            with obs.call("spark.sql", target):
                # collect(), never iteration: pyspark 4 yields Column objects
                # when a DataFrame is iterated, and a Column's missing
                # attribute reads as `'Column' object is not callable`.
                rows = self.spark.sql(statement).collect()
                return [dict(row.asDict()) for row in rows]

        if self.warehouse_id is None:
            raise ValueError("warehouse_id is required for SQL calls")

        with obs.call("sql", target):
            resp = with_retry(
                lambda: self.w.statement_execution.execute_statement(
                    warehouse_id=self.warehouse_id,
                    statement=statement,
                    wait_timeout="50s",
                    on_wait_timeout=(
                        ExecuteStatementRequestOnWaitTimeout.CANCEL),
                ),
                kind="sql", target=target)
            state = resp.status.state.value if resp.status and resp.status.state else "?"
            if state != "SUCCEEDED":
                message = (resp.status.error.message
                           if resp.status and resp.status.error else state)
                raise RuntimeError(message)

            if resp.manifest and resp.manifest.truncated:
                raise RuntimeError(
                    f"the warehouse truncated the result for {target}; "
                    f"the answer would be incomplete")

            columns = [c.name for c in resp.manifest.schema.columns]
            rows = list((resp.result.data_array if resp.result else None) or [])
            chunk = resp.result.next_chunk_index if resp.result else None
            while chunk is not None:
                page = with_retry(
                    lambda c=chunk: (
                        self.w.statement_execution
                        .get_statement_result_chunk_n(resp.statement_id, c)),
                    kind="sql", target=target)
                rows.extend(page.data_array or [])
                chunk = page.next_chunk_index

            return [dict(zip(columns, row)) for row in rows]


def workspace_from_env() -> "Workspace":
    """Retired: resolution now lives in Workspace() itself."""
    return Workspace()


def failure_category(exc: Exception) -> str:
    """Classify an SDK/SQL error as 'permission', 'not_found', 'mv_no_history', or 'other'."""
    text = str(exc).lower()
    if ("permission" in text or "does not have" in text or "403" in text
            or "401" in text or "unauthorized" in text):
        return "permission"
    if "does not exist" in text or "not found" in text or "404" in text:
        return "not_found"
    if "expect_table_not_view" in text:
        return "mv_no_history"
    return "other"


#: The HTTP statuses that mark a passing failure. A retry can clear each one.
_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})

#: The words that mark a passing failure when no status code is present.
_TRANSIENT_WORDS = ("rate limit", "temporarily unavailable", "connection",
                    "timed out", "timeout", "canceled")

#: The wait between attempts. Tests replace this with a recorder, so no test
#: waits for real.
_sleep = time.sleep


def is_transient(exc: Exception) -> bool:
    """Say whether a failed read is worth one more attempt.

    The status code decides when the exception carries one. The error text
    decides only when there is no status code. A permission error, a missing
    object, a SQL syntax error, and the materialized-view rejection are all
    permanent. The caller must fail closed on each of them at once.
    """
    status = getattr(exc, "status_code", None)
    if status is not None:
        try:
            return int(status) in _TRANSIENT_STATUS
        except (TypeError, ValueError):
            return False
    text = str(exc).lower()
    if "expect_table_not_view" in text:
        return False
    return any(word in text for word in _TRANSIENT_WORDS)


def _retry_wait(exc: Exception, computed: float) -> float:
    """The larger of the exception's retry-after value and the computed wait.

    A retry-after value is advisory; the computed wait is the floor. An
    unreadable or absent value leaves the computed wait in place.
    """
    for name in ("retry_after", "retry_after_seconds", "Retry-After"):
        value = getattr(exc, name, None)
        if value is None:
            continue
        try:
            return max(computed, float(value))
        except (TypeError, ValueError):
            continue
    return computed


def with_retry(fn, *, kind: str, target: str, attempts: int = 3,
               sleep=None):
    """Call `fn`. Retry a passing failure. Give up on a lasting one.

    Three attempts in total. The waits are 1.0s, then 2.0s, with no jitter, so
    a test can pin every wait. A permanent failure raises at once, with no
    retry. `sleep` is injectable; a test passes a recorder and never waits.

    This helper is for reads only. Never use it to wrap a write.
    """
    if sleep is None:
        sleep = _sleep
    wait = 1.0
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt >= attempts or not is_transient(exc):
                raise
            pause = _retry_wait(exc, wait)
            obs.retry(kind, target, attempt, pause,
                      f"{type(exc).__name__}: {exc}")
            sleep(pause)
            wait *= 2


def parse_json_field(value: Any) -> dict | None:
    """Parse a JSON string or pass through a dict. None if absent or unparseable."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


# --- history readers ---------------------------------------------------------

def _from_epoch(value):
    """Seconds since the epoch → an aware UTC datetime.

    Timestamps are read as epochs rather than as the API's rendered string
    because a Spark TIMESTAMP is an instant, and `unix_timestamp` returns that
    instant regardless of the warehouse's session timezone. The rendered string
    carries a "Z" suffix whose accuracy depends on that session setting, which
    is warehouse configuration this tool does not control. Verified identical to
    the string path on a UTC warehouse; correct on any other.
    """
    from datetime import timezone
    return datetime.fromtimestamp(int(value), tz=timezone.utc)


def read_delta_history(ws: "Workspace", table: str, *,
                       with_metrics: bool = True,
                       since_epoch: int | None = None) -> list:
    """Every commit for a physical Delta table, newest first.

    `with_metrics=False` drops the wide operationMetrics column. The gate only
    classifies commits, so it never needs it; analyze() does. A `since_epoch`
    keeps only commits at or after that epoch second, so SQL returns a small
    window instead of the whole history.
    """
    from .models import Commit
    metrics = ", operationMetrics" if with_metrics else ""
    where = (f"WHERE unix_timestamp(timestamp) >= {int(since_epoch)} "
             if since_epoch is not None else "")
    rows = ws.sql(
        "SELECT version, unix_timestamp(timestamp) AS ts_epoch, operation, "
        f"operationParameters{metrics} "
        f"FROM (DESCRIBE HISTORY {quote_name(table)}) "
        f"{where}"
        "ORDER BY version DESC",
        target=table)
    return [
        Commit(
            version=int(r.get("version") or 0),
            timestamp=_from_epoch(r["ts_epoch"]),
            operation=str(r.get("operation") or ""),
            params=parse_json_field(r.get("operationParameters")) or {},
            metrics=(parse_json_field(r.get("operationMetrics"))
                     if with_metrics else None),
        )
        for r in rows
    ]


read_history = read_delta_history


def read_mv_refreshes(ws: "Workspace", mv: str, *,
                      since_epoch: int | None = None) -> list:
    """Completed MV refreshes from the pipeline event log.

    DESCRIBE HISTORY is rejected on a materialized view, so this is the only
    way to see when it last refreshed. A `since_epoch` keeps only refreshes at
    or after that epoch second.
    """
    from .models import Commit
    statement = (
        "SELECT unix_timestamp(timestamp) AS ts_epoch "
        f"FROM event_log(TABLE({quote_name(mv)})) "
        "WHERE event_type = 'update_progress' "
        "AND get_json_object(details, '$.update_progress.state') = 'COMPLETED'"
    )
    if since_epoch is not None:
        statement += f" AND unix_timestamp(timestamp) >= {int(since_epoch)}"
    rows = ws.sql(statement, target=mv)
    return [
        Commit(version=i, timestamp=_from_epoch(r["ts_epoch"]),
               operation="MV REFRESH", params={}, metrics=None)
        for i, r in enumerate(rows)
    ]


def read_commits(ws: "Workspace", table: str, *,
                 with_metrics: bool = True,
                 since_epoch: int | None = None) -> list:
    """History for any physical object: a table, streaming table, or MV.

    DESCRIBE HISTORY covers tables and streaming tables. A materialized view
    rejects it, so fall back to the pipeline event log. A `since_epoch` is
    forwarded to both readers.
    """
    try:
        return read_delta_history(ws, table, with_metrics=with_metrics,
                                  since_epoch=since_epoch)
    except Exception as exc:
        if "expect_table_not_view" in str(exc).lower():
            with obs.call("mv.event_log", table):
                return read_mv_refreshes(ws, table, since_epoch=since_epoch)
        raise


def read_latest_commit(ws: "Workspace", table: str, *,
                       not_after: int | None = None,
                       since_epoch: int | None = None) -> datetime | None:
    """The newest qualifying write for a table or an MV, or None.

    This is the gate's one question. A write dated after `not_after` (epoch
    seconds) is ignored, so a clock-skewed future commit cannot make a stale
    table look fresh. A `since_epoch` keeps the read small.
    """
    newest: datetime | None = None
    for commit in read_commits(ws, table, with_metrics=False,
                               since_epoch=since_epoch):
        if not qualifying(commit):
            continue
        if not_after is not None and commit.timestamp.timestamp() > not_after:
            continue
        if newest is None or commit.timestamp > newest:
            newest = commit.timestamp
    return newest
