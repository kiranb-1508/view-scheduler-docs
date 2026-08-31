"""The I/O layer. Everything that leaves the process lives here.

READ ONLY: metadata reads and DESCRIBE HISTORY / event log queries. Nothing in
this module writes, alters, optimizes, vacuums or drops anything.
"""

from __future__ import annotations

import json
import random
import time
from typing import Any, Callable

from databricks.sdk import WorkspaceClient

from . import obs

RETRYABLE = (429, 500, 502, 503, 504)
MAX_ATTEMPTS = 5


def _status_of(exc: Exception) -> int | None:
    for attr in ("status_code", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None


def with_backoff(fn: Callable[[], Any], target: str, kind: str) -> Any:
    """Retry REST calls on 429/5xx with exponential backoff and jitter."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with obs.call(kind, target, attempt=attempt):
                return fn()
        except Exception as exc:
            if _status_of(exc) not in RETRYABLE or attempt == MAX_ATTEMPTS:
                raise
            time.sleep(min(2 ** attempt, 30) * (0.5 + random.random()))
    raise AssertionError("unreachable")


class Workspace:
    """Thin wrapper over the SDK. One method per external call we make."""

    def __init__(self, client: WorkspaceClient | None = None, warehouse_id: str | None = None):
        self.w = client or WorkspaceClient()
        self.warehouse_id = warehouse_id

    # --- Unity Catalog -------------------------------------------------------

    def get_table(self, full_name: str) -> dict:
        """Raw GET /api/2.1/unity-catalog/tables/{full_name}.

        Deliberately raw rather than the typed SDK object: view_dependencies
        nests the name under a "table" key, and keeping the wire shape means
        the fixtures and the production path agree.
        """
        return with_backoff(
            lambda: self.w.api_client.do(
                "GET", f"/api/2.1/unity-catalog/tables/{full_name}"),
            target=full_name, kind="uc.tables.get")

    # --- SQL -----------------------------------------------------------------

    def sql(self, statement: str, target: str) -> list[dict[str, Any]]:
        """Run a read-only statement, return rows as dicts of strings."""
        if self.warehouse_id is None:
            raise ValueError("warehouse_id is required for SQL calls")

        def run():
            resp = self.w.statement_execution.execute_statement(
                warehouse_id=self.warehouse_id, statement=statement,
                wait_timeout="50s")
            state = resp.status.state.value if resp.status and resp.status.state else "?"
            if state != "SUCCEEDED":
                message = (resp.status.error.message
                           if resp.status and resp.status.error else state)
                raise RuntimeError(message)
            columns = [c.name for c in resp.manifest.schema.columns]
            data = (resp.result.data_array if resp.result else None) or []
            return [dict(zip(columns, row)) for row in data]

        return with_backoff(run, target=target, kind="sql")


def parse_json_field(value: Any) -> dict | None:
    """operationParameters/operationMetrics arrive as JSON strings over the SQL
    API and as maps over Spark. None means the field was absent entirely."""
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

def _parse_ts(value: str):
    from datetime import datetime, timezone
    text = (value or "").replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def read_history(ws: "Workspace", table: str) -> list:
    """DESCRIBE HISTORY for a physical Delta table."""
    from .models import Commit
    rows = ws.sql(f"DESCRIBE HISTORY {table}", target=table)
    return [
        Commit(
            version=int(r.get("version") or 0),
            timestamp=_parse_ts(r["timestamp"]),
            operation=r.get("operation") or "",
            params=parse_json_field(r.get("operationParameters")) or {},
            metrics=parse_json_field(r.get("operationMetrics")),
        )
        for r in rows
    ]


def read_mv_refreshes(ws: "Workspace", mv: str) -> list:
    """Completed refreshes of a materialized view.

    DESCRIBE HISTORY is rejected on a materialized view
    (EXPECT_TABLE_NOT_VIEW.NO_ALTERNATIVE), so refresh times come from the
    pipeline event log instead. Two consequences worth knowing:

      * The event log is per-pipeline, so update_progress/COMPLETED is the
        pipeline finishing, not this table specifically. It is a conservative
        proxy — never earlier than the table was ready.
      * By default only the pipeline's run-as user can read it. A caller
        without that access gets a permission error, which the caller turns
        into a skipped entry rather than a crash.
    """
    from .models import Commit
    statement = (
        "SELECT timestamp, "
        "get_json_object(details, '$.update_progress.state') AS state "
        f"FROM event_log(TABLE({mv})) "
        "WHERE event_type = 'update_progress' "
        "AND get_json_object(details, '$.update_progress.state') = 'COMPLETED'"
    )
    rows = ws.sql(statement, target=mv)
    return [
        Commit(version=i, timestamp=_parse_ts(r["timestamp"]),
               operation="MV REFRESH", params={}, metrics=None)
        for i, r in enumerate(rows)
    ]
