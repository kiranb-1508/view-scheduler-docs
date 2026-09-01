"""I/O layer. Read-only: metadata lookups, DESCRIBE HISTORY, event log queries."""

from __future__ import annotations

import json
from typing import Any

from databricks.sdk import WorkspaceClient

from . import obs

class Workspace:
    """Thin wrapper over the SDK. One method per external call we make."""

    def __init__(self, client: WorkspaceClient | None = None, warehouse_id: str | None = None):
        self.w = client or WorkspaceClient()
        self.warehouse_id = warehouse_id

    # --- Unity Catalog -------------------------------------------------------

    def get_table(self, full_name: str) -> dict:
        """Raw GET for a single table's metadata including view_dependencies."""
        with obs.call("uc.tables.get", full_name):
            return self.w.api_client.do(
                "GET", f"/api/2.1/unity-catalog/tables/{full_name}")

    # --- SQL -----------------------------------------------------------------

    def sql(self, statement: str, target: str) -> list[dict[str, Any]]:
        """Run a read-only statement, return rows as dicts of strings."""
        if self.warehouse_id is None:
            raise ValueError("warehouse_id is required for SQL calls")

        with obs.call("sql", target):
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


def failure_category(exc: Exception) -> str:
    """Classify an SDK/SQL error as 'permission', 'not_found', 'mv_no_history', or 'other'."""
    text = str(exc).lower()
    if "permission" in text or "does not have" in text or "403" in text:
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
    """Read completed MV refreshes from the pipeline event log (DESCRIBE HISTORY is rejected on MVs)."""
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
