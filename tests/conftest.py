"""Shared test scaffolding.

FakeWorkspace stands in for view_scheduler.client.Workspace so the orchestration
and history-reading paths — everything reached only against a live warehouse —
can be exercised offline. It dispatches sql() on the statement text so the same
table can answer DESCRIBE HISTORY and event_log() differently, which is exactly
what the materialized-view fallback in run._read depends on.
"""

from __future__ import annotations

from typing import Any


class FakeWorkspace:
    def __init__(self, tables: dict[str, dict] | None = None,
                 history: dict[str, list[dict]] | None = None,
                 mv_events: dict[str, list[dict]] | None = None,
                 history_errors: dict[str, Exception] | None = None,
                 table_errors: dict[str, Exception] | None = None):
        self._tables = tables or {}
        self._history = history or {}            # table -> DESCRIBE HISTORY rows
        self._mv_events = mv_events or {}         # table -> event_log rows
        self._history_errors = history_errors or {}  # raised on DESCRIBE HISTORY
        self._table_errors = table_errors or {}      # raised on get_table
        self.warehouse_id = "fake"

    def get_table(self, full_name: str) -> dict:
        if full_name in self._table_errors:
            raise self._table_errors[full_name]
        if full_name not in self._tables:
            raise KeyError(f"Table '{full_name}' does not exist")
        return self._tables[full_name]

    def sql(self, statement: str, target: str) -> list[dict[str, Any]]:
        if "event_log" in statement:
            return self._mv_events.get(target, [])
        # DESCRIBE HISTORY path
        if target in self._history_errors:
            raise self._history_errors[target]
        return self._history.get(target, [])
