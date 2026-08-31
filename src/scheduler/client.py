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
