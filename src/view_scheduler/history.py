"""Classify commits and extract per-day finish times. Pure — no I/O."""

from __future__ import annotations

import statistics
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Iterable

from .models import Commit, TableHistory

#: A table with at least this many qualifying commits on a typical day is
#: refreshed too often for "first commit of the day" to mean "when it was ready"
#: — it looks continuous (streaming, CDC, micro-batch). Flagged, not modelled.
CONTINUOUS_COMMITS_PER_DAY = 6

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

FULL_REPLACE_CLASS = "full_replace"
INCREMENTAL_CLASS = "incremental"
IGNORE_CLASS = "ignore"
#: An operation name in none of the sets above. Not counted as a refresh (the
#: conservative choice), but named in the report rather than silently dropped —
#: Delta adds operation names over time, and a dropped early write pushes the
#: finish time later with no signal that anything was missed.
UNKNOWN_CLASS = "unknown"


def classify(commit: Commit) -> str:
    """Which class this commit falls into: full_replace, incremental, ignore, unknown."""
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
    return UNKNOWN_CLASS


def unknown_operations(commits: Iterable[Commit]) -> tuple[str, ...]:
    """Operation names we did not recognise, in stable order."""
    return tuple(sorted({
        (c.operation or "").upper() for c in commits
        if classify(c) == UNKNOWN_CLASS
    }))


def has_metrics(commit: Commit) -> bool:
    """False when the writer emitted no usable metrics (None or empty dict)."""
    return bool(commit.metrics)


def qualifying(commit: Commit) -> bool:
    """Any write (full replace or incremental, including 0-row) counts as a refresh.

    An unrecognised operation does not count — the same conservative verdict as
    maintenance, but `unknown_operations` names it so the silence is broken.
    """
    return classify(commit) in (FULL_REPLACE_CLASS, INCREMENTAL_CLASS)


def is_degraded(commits: Iterable[Commit]) -> bool:
    """True when any qualifying incremental commit had no usable metrics."""
    return any(
        classify(c) == INCREMENTAL_CLASS and not has_metrics(c) for c in commits
    )


def finish_times(commits: Iterable[Commit], today: date, days: int,
                 tz) -> dict[date, datetime]:
    """First qualifying commit of each local day → {date: timestamp}."""
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


def qualifying_per_day(commits: Iterable[Commit], today: date, days: int,
                       tz) -> dict[date, int]:
    """How many qualifying commits landed on each day in the window."""
    cutoff = today - timedelta(days=days)
    counts: Counter[date] = Counter()
    for commit in commits:
        if not qualifying(commit):
            continue
        day = commit.timestamp.astimezone(tz).date()
        if day <= cutoff or day > today:
            continue
        counts[day] += 1
    return dict(counts)


def looks_continuous(commits: Iterable[Commit], today: date, days: int,
                     tz) -> bool:
    """True when the median commits/day >= CONTINUOUS_COMMITS_PER_DAY."""
    per_day = qualifying_per_day(commits, today, days, tz)
    if not per_day:
        return False
    return statistics.median(per_day.values()) >= CONTINUOUS_COMMITS_PER_DAY


def summarize(name: str, commits: list[Commit], today: date, days: int,
              tz) -> TableHistory:
    """Roll one table's commits into the shape the advisor needs.

    Keeps the first qualifying finish of each local day, and flags the two
    facts that weaken a number: writes with no metrics, and a table that runs
    so often that its "finish" is only a floor.
    """
    return TableHistory(
        name=name,
        finish_times=finish_times(commits, today, days, tz),
        metrics_degraded=is_degraded(commits),
        continuous=looks_continuous(commits, today, days, tz),
        unknown_ops=unknown_operations(commits),
    )
