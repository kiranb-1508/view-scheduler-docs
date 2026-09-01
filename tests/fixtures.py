"""Fake data in the shapes the real APIs actually return.

Every shape here was captured from a live workspace, not from the design docs.
Two differences from the docs are load-bearing:

  * view_dependencies nests the name under a "table" key. The design pseudocode
    reads d["table_full_name"] directly, which finds nothing and silently
    classifies every dependency as a function dependency.
  * DESCRIBE HISTORY via the SQL Statement API returns operationParameters and
    operationMetrics as JSON *strings*, not maps.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

UTC = timezone.utc


# --- Unity Catalog Tables API ------------------------------------------------

def uc_table(full_name: str, table_type: str = "MANAGED") -> dict[str, Any]:
    return {"full_name": full_name, "table_type": table_type}


def uc_view(full_name: str, depends_on: list[str], functions: int = 0) -> dict[str, Any]:
    """A VIEW payload. depends_on are table full names; functions adds that many
    function dependencies, which carry no "table" key at all."""
    deps: list[dict[str, Any]] = [
        {"metastore_name": "fixture-metastore", "table": {"table_full_name": n}}
        for n in depends_on
    ]
    for i in range(functions):
        deps.append(
            {"metastore_name": "fixture-metastore",
             "function": {"function_full_name": f"main.default.udf_{i}"}}
        )
    return {
        "full_name": full_name,
        "table_type": "VIEW",
        "view_dependencies": {"dependencies": deps},
    }


#: top -> (base_b, mid1, mid2); mid1 -> base_a; mid2 -> base_a.
#: A diamond: base_a is reachable by two paths and must be walked exactly once.
DIAMOND: dict[str, dict[str, Any]] = {
    "c.s.v_top": uc_view("c.s.v_top", ["c.s.base_b", "c.s.v_mid1", "c.s.v_mid2"]),
    "c.s.v_mid1": uc_view("c.s.v_mid1", ["c.s.base_a"]),
    "c.s.v_mid2": uc_view("c.s.v_mid2", ["c.s.base_a"]),
    "c.s.base_a": uc_table("c.s.base_a"),
    "c.s.base_b": uc_table("c.s.base_b"),
}

#: a -> b -> a. View-level cycles cannot exist in UC, but the guard is what
#: stops a malformed or stale metadata response from hanging the walk.
CYCLE: dict[str, dict[str, Any]] = {
    "c.s.v_a": uc_view("c.s.v_a", ["c.s.v_b"]),
    "c.s.v_b": uc_view("c.s.v_b", ["c.s.v_a"]),
}


# --- DESCRIBE HISTORY --------------------------------------------------------

def raw_history_row(
    version: int,
    ts: datetime,
    operation: str,
    params: dict | None = None,
    metrics: dict | None = None,
) -> dict[str, Any]:
    """A row as the SQL Statement API returns it: nested fields JSON-encoded.

    The reader selects `unix_timestamp(timestamp) AS ts_epoch` rather than the
    rendered timestamp string, so that is the column modelled here — an epoch is
    the same instant whatever the warehouse's session timezone renders.
    """
    return {
        "version": str(version),
        "ts_epoch": str(int(ts.astimezone(UTC).timestamp())),
        "operation": operation,
        "operationParameters": json.dumps(params) if params is not None else None,
        "operationMetrics": json.dumps(metrics) if metrics is not None else None,
    }


def at(day: str, hhmm: str) -> datetime:
    """'2026-08-06', '04:12' -> aware UTC datetime."""
    return datetime.fromisoformat(f"{day}T{hhmm}:00").replace(tzinfo=UTC)


#: One of every operation class Step 2 has to classify. Verified against a real
#: table: plain WRITE with a mode is by far the commonest, and CREATE OR REPLACE
#: TABLE really does come back with metrics of {}.
OPERATION_ZOO: list[dict[str, Any]] = [
    raw_history_row(9, at("2026-08-06", "11:17"), "OPTIMIZE",
                    {"predicate": "[]", "auto": "false"}, {"numRemovedFiles": "3"}),
    raw_history_row(8, at("2026-08-06", "10:00"), "VACUUM START", {}, {}),
    raw_history_row(7, at("2026-08-06", "09:00"), "DELETE",
                    {"predicate": "[]"}, {"numDeletedRows": "5"}),
    raw_history_row(6, at("2026-08-06", "08:00"), "WRITE",
                    {"mode": "Append", "partitionBy": "[]"},
                    {"numFiles": "1", "numOutputRows": "1"}),
    raw_history_row(5, at("2026-08-06", "07:00"), "WRITE",
                    {"mode": "Append"}, {"numOutputRows": "0"}),
    raw_history_row(4, at("2026-08-06", "06:00"), "WRITE",
                    {"mode": "Overwrite"}, {"numOutputRows": "0"}),
    raw_history_row(3, at("2026-08-06", "05:00"), "MERGE",
                    {"predicate": "true"}, {"numTargetRowsInserted": "12"}),
    raw_history_row(2, at("2026-08-06", "04:00"), "UPDATE",
                    {"predicate": "x > 1"}, None),
    raw_history_row(1, at("2026-08-06", "03:00"), "CREATE OR REPLACE TABLE",
                    {"isManaged": "true"}, {}),
    raw_history_row(0, at("2026-08-06", "02:00"), "SET TBLPROPERTIES", {}, {}),
]


# --- Materialized view event log --------------------------------------------

def mv_event_rows(completions: list[datetime], noise: bool = True) -> list[dict[str, Any]]:
    """event_log(TABLE(mv)) rows. Only update_progress/COMPLETED counts."""
    rows: list[dict[str, Any]] = []
    for ts in completions:
        if noise:
            rows.append({"timestamp": (ts - timedelta(seconds=30)).strftime(
                "%Y-%m-%dT%H:%M:%S.000Z"), "event_type": "update_progress",
                "state": "RUNNING"})
            rows.append({"timestamp": (ts - timedelta(seconds=10)).strftime(
                "%Y-%m-%dT%H:%M:%S.000Z"), "event_type": "flow_progress",
                "state": None})
        rows.append({"timestamp": ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                     "event_type": "update_progress", "state": "COMPLETED"})
    return rows
