"""I/O layer. Read-only: metadata lookups, DESCRIBE HISTORY, event log queries."""

from __future__ import annotations

import json
import os
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
        """Raw GET for one table's metadata, including view_dependencies."""
        path = quote(full_name, safe=".")
        with obs.call("uc.tables.get", full_name):
            return self.w.api_client.do(
                "GET", f"/api/2.1/unity-catalog/tables/{path}")

    # --- SQL -----------------------------------------------------------------

    def sql(self, statement: str, target: str) -> list[dict[str, Any]]:
        """Run a read-only statement. Return every row as a dict of strings.

        Two engines, one seam. A SparkSession runs the statement on the local
        compute; otherwise the Statement API runs it on a warehouse. The rows
        must be interchangeable: same columns, same order, same facts.

        The statement path also follows result chunking to the end and refuses
        a truncated result, so a caller never silently sees only the first
        page of a large result.
        """
        if self.spark is not None:
            with obs.call("spark.sql", target):
                return [dict(row.asDict()) for row in self.spark.sql(statement)]

        if self.warehouse_id is None:
            raise ValueError("warehouse_id is required for SQL calls")

        with obs.call("sql", target):
            resp = self.w.statement_execution.execute_statement(
                warehouse_id=self.warehouse_id,
                statement=statement,
                wait_timeout="50s",
                on_wait_timeout=ExecuteStatementRequestOnWaitTimeout.CANCEL,
            )
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
                page = self.w.statement_execution.get_statement_result_chunk_n(
                    resp.statement_id, chunk)
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
                       with_metrics: bool = True) -> list:
    """Every commit for a physical Delta table, newest first.

    `with_metrics=False` drops the wide operationMetrics column. The gate only
    classifies commits, so it never needs it; analyze() does.
    """
    from .models import Commit
    metrics = ", operationMetrics" if with_metrics else ""
    rows = ws.sql(
        "SELECT version, unix_timestamp(timestamp) AS ts_epoch, operation, "
        f"operationParameters{metrics} "
        f"FROM (DESCRIBE HISTORY {quote_name(table)}) "
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


def read_mv_refreshes(ws: "Workspace", mv: str) -> list:
    """Completed MV refreshes from the pipeline event log.

    DESCRIBE HISTORY is rejected on a materialized view, so this is the only
    way to see when it last refreshed.
    """
    from .models import Commit
    statement = (
        "SELECT unix_timestamp(timestamp) AS ts_epoch "
        f"FROM event_log(TABLE({quote_name(mv)})) "
        "WHERE event_type = 'update_progress' "
        "AND get_json_object(details, '$.update_progress.state') = 'COMPLETED'"
    )
    rows = ws.sql(statement, target=mv)
    return [
        Commit(version=i, timestamp=_from_epoch(r["ts_epoch"]),
               operation="MV REFRESH", params={}, metrics=None)
        for i, r in enumerate(rows)
    ]


def read_commits(ws: "Workspace", table: str, *,
                 with_metrics: bool = True) -> list:
    """History for any physical object: a table, streaming table, or MV.

    DESCRIBE HISTORY covers tables and streaming tables. A materialized view
    rejects it, so fall back to the pipeline event log.
    """
    try:
        return read_delta_history(ws, table, with_metrics=with_metrics)
    except Exception as exc:
        if "expect_table_not_view" in str(exc).lower():
            with obs.call("mv.event_log", table):
                return read_mv_refreshes(ws, table)
        raise


def read_latest_commit(ws: "Workspace", table: str, *,
                       not_after: int | None = None) -> datetime | None:
    """The newest qualifying write for a table or an MV, or None.

    This is the gate's one question. A write dated after `not_after` (epoch
    seconds) is ignored, so a clock-skewed future commit cannot make a stale
    table look fresh.
    """
    newest: datetime | None = None
    for commit in read_commits(ws, table, with_metrics=False):
        if not qualifying(commit):
            continue
        if not_after is not None and commit.timestamp.timestamp() > not_after:
            continue
        if newest is None or commit.timestamp > newest:
            newest = commit.timestamp
    return newest
