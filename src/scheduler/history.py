"""Step 2 — decide which commits count as a refresh, and when each day finished.

The step most likely to be silently wrong. Operation class comes from
operationParameters["mode"], not the operation name: the commonest name is a
plain WRITE, and only its mode says whether it replaced the table or appended
to it. Classify by name and most tables read as dormant — a confident wrong
answer, not an error.

Everything in this module is pure. Nothing reads the wall clock; `today` is
passed in.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterable

from .models import Commit, TableHistory

#: Names that always mean the table was rebuilt from scratch.
FULL_REPLACE = {
    "CREATE OR REPLACE TABLE",
    "CREATE OR REPLACE TABLE AS SELECT",
    "CREATE TABLE AS SELECT",
    "REPLACE TABLE AS SELECT",
    "INSERT OVERWRITE",
    "TRUNCATE AND INSERT",
}

#: Names that mean rows were added or changed.
INCREMENTAL = {"MERGE", "UPDATE", "COPY INTO", "STREAMING UPDATE", "INSERT"}

#: Maintenance and metadata. Never a refresh, whatever the metrics say.
IGNORE = {
    "OPTIMIZE", "VACUUM", "VACUUM START", "VACUUM END", "DELETE", "TRUNCATE",
    "SET TBLPROPERTIES", "UNSET TBLPROPERTIES", "CHANGE COLUMN", "ADD COLUMNS",
    "RESTORE", "CLONE", "ADD CONSTRAINT", "DROP CONSTRAINT", "LIQUID CLUSTERING",
    "RENAME COLUMN", "UPGRADE PROTOCOL", "SET COLUMN COMMENT",
}

#: Metric keys that each mean "rows landed".
ROW_KEYS = (
    "numOutputRows", "numTargetRowsInserted", "numTargetRowsUpdated",
    "numTargetRowsDeleted", "numTargetRowsCopied", "numCopiedRows",
    "numUpdatedRows", "numInsertedRows", "numAddedRows",
)

FULL_REPLACE_CLASS = "full_replace"
INCREMENTAL_CLASS = "incremental"
IGNORE_CLASS = "ignore"


def classify(commit: Commit) -> str:
    """Which of the three classes this commit falls into."""
    operation = (commit.operation or "").upper()
    mode = (commit.params or {}).get("mode")

    if operation in IGNORE:
        return IGNORE_CLASS
    if operation in FULL_REPLACE or (operation == "WRITE" and mode == "Overwrite"):
        return FULL_REPLACE_CLASS
    if operation in INCREMENTAL or (operation == "WRITE" and mode == "Append"):
        return INCREMENTAL_CLASS
    if operation == "WRITE":
        # A WRITE with no mode at all. Non-Spark writers do this; treating it as
        # maintenance would mark a live table dormant, so it counts.
        return INCREMENTAL_CLASS
    if operation == "MV REFRESH":
        return FULL_REPLACE_CLASS
    return IGNORE_CLASS


def rows_written(metrics: dict[str, str] | None) -> int:
    if not metrics:
        return 0
    total = 0
    for key in ROW_KEYS:
        try:
            total += int(metrics.get(key, 0) or 0)
        except (TypeError, ValueError):
            continue
    return total


def has_metrics(commit: Commit) -> bool:
    """False when the writer emitted no usable metrics at all.

    ADF and Synapse Link write through non-Spark paths and may emit nothing, or
    an empty map. Both are degraded: we count the commit and flag the table.
    """
    return bool(commit.metrics)


def qualifying(commit: Commit) -> bool:
    """Did this commit refresh the table?"""
    kind = classify(commit)
    if kind == IGNORE_CLASS:
        return False
    if kind == FULL_REPLACE_CLASS:
        return True          # a recompute that produced zero rows still ran
    if not has_metrics(commit):
        return True          # degraded — counted, and the table gets flagged
    return rows_written(commit.metrics) > 0


def is_degraded(commits: Iterable[Commit]) -> bool:
    """True when any qualifying incremental commit had no usable metrics."""
    return any(
        classify(c) == INCREMENTAL_CLASS and not has_metrics(c) for c in commits
    )


def finish_times(commits: Iterable[Commit], today: date, days: int,
                 tz) -> dict[date, datetime]:
    """First qualifying commit of each day, as {local date: local timestamp}.

    First, not last: it is the earliest moment the table was ready. Days are
    local days in the configured timezone, which is why tz is required rather
    than assumed.
    """
    cutoff = today - timedelta(days=days)
    by_day: dict[date, datetime] = {}
    for commit in sorted(commits, key=lambda c: c.timestamp):
        if not qualifying(commit):
            continue
        local = commit.timestamp.astimezone(tz)
        day = local.date()
        if day <= cutoff or day > today:
            continue
        by_day.setdefault(day, local)
    return by_day


def summarize(name: str, commits: list[Commit], today: date, days: int,
              tz) -> TableHistory:
    return TableHistory(
        name=name,
        finish_times=finish_times(commits, today, days, tz),
        metrics_degraded=is_degraded(commits),
    )
