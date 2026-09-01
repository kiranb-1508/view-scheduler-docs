"""Phase 2 gate, unit half. Every operation class, plus the two branches that
decide whether a live table reads as dormant."""

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from scheduler.client import parse_json_field
from scheduler.history import (
    classify, finish_times, is_degraded, qualifying, summarize,
)
from scheduler.models import Commit
from tests import fixtures as f

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc


def commit(operation, params=None, metrics=None, ts=None, version=1) -> Commit:
    return Commit(version=version, timestamp=ts or f.at("2026-08-06", "04:00"),
                  operation=operation, params=params or {}, metrics=metrics)


def from_raw(row: dict) -> Commit:
    """Convert a raw SQL-API row exactly as the production reader does."""
    return Commit(
        version=int(row["version"]),
        timestamp=datetime.strptime(row["timestamp"], "%Y-%m-%dT%H:%M:%S.000Z").replace(tzinfo=UTC),
        operation=row["operation"],
        params=parse_json_field(row["operationParameters"]) or {},
        metrics=parse_json_field(row["operationMetrics"]),
    )


# --- classification ----------------------------------------------------------

@pytest.mark.parametrize("operation,params,expected", [
    ("WRITE", {"mode": "Overwrite"}, "full_replace"),
    ("WRITE", {"mode": "Append"}, "incremental"),
    ("CREATE OR REPLACE TABLE", {}, "full_replace"),
    ("INSERT OVERWRITE", {}, "full_replace"),
    ("MERGE", {}, "incremental"),
    ("UPDATE", {}, "incremental"),
    ("COPY INTO", {}, "incremental"),
    ("STREAMING UPDATE", {}, "incremental"),
    ("OPTIMIZE", {}, "ignore"),
    ("VACUUM START", {}, "ignore"),
    ("DELETE", {}, "ignore"),
    ("TRUNCATE", {}, "ignore"),
    ("SET TBLPROPERTIES", {}, "ignore"),
])
def test_classification(operation, params, expected):
    assert classify(commit(operation, params)) == expected


def test_the_docs_headline_bug_write_is_not_classified_by_name():
    """The same operation name, opposite classes. Name alone is not enough."""
    assert classify(commit("WRITE", {"mode": "Overwrite"})) != \
           classify(commit("WRITE", {"mode": "Append"}))


def test_write_with_no_mode_still_counts():
    """Non-Spark writers omit mode. Ignoring it would mark the table dormant."""
    assert qualifying(commit("WRITE", {}, {"numOutputRows": "5"}))


# --- qualifying --------------------------------------------------------------

def test_zero_row_full_replace_qualifies():
    assert qualifying(commit("WRITE", {"mode": "Overwrite"}, {"numOutputRows": "0"}))


def test_zero_row_incremental_still_qualifies():
    """A quiet day with no new data: the incremental pipeline still ran, so it
    counts as refreshed (deliberate — a genuinely-skipped day is indistinguishable
    from the commit log)."""
    assert qualifying(commit("WRITE", {"mode": "Append"}, {"numOutputRows": "0"}))
    assert qualifying(commit("MERGE", {}, {"numTargetRowsUpdated": "0"}))


def test_incremental_with_absent_metrics_qualifies_and_flags():
    degraded = commit("MERGE", {}, None)
    assert qualifying(degraded)
    assert is_degraded([degraded])


def test_incremental_with_empty_metrics_qualifies_and_flags():
    degraded = commit("WRITE", {"mode": "Append"}, {})
    assert qualifying(degraded)
    assert is_degraded([degraded])


def test_full_replace_with_empty_metrics_does_not_flag_as_degraded():
    """CREATE OR REPLACE TABLE really does emit {} — that is normal, not degraded."""
    assert not is_degraded([commit("CREATE OR REPLACE TABLE", {}, {})])


def test_maintenance_never_qualifies_however_many_rows_it_touched():
    assert not qualifying(commit("DELETE", {}, {"numDeletedRows": "9999"}))
    assert not qualifying(commit("OPTIMIZE", {}, {"numOutputRows": "9999"}))


def test_operation_zoo_end_to_end():
    commits = [from_raw(r) for r in f.OPERATION_ZOO]
    qualified = {c.operation for c in commits if qualifying(c)}
    assert qualified == {"WRITE", "MERGE", "UPDATE", "CREATE OR REPLACE TABLE"}
    ignored = {c.operation for c in commits if not qualifying(c)}
    assert {"OPTIMIZE", "VACUUM START", "DELETE", "SET TBLPROPERTIES"} <= ignored


# --- finish times ------------------------------------------------------------

def test_first_qualifying_commit_of_the_day_wins():
    commits = [
        commit("WRITE", {"mode": "Overwrite"}, {}, f.at("2026-08-06", "06:00"), 2),
        commit("WRITE", {"mode": "Overwrite"}, {}, f.at("2026-08-06", "04:00"), 1),
        commit("OPTIMIZE", {}, {}, f.at("2026-08-06", "03:00"), 0),
    ]
    times = finish_times(commits, date(2026, 8, 6), 30, UTC)
    assert times[date(2026, 8, 6)].hour == 4


def test_days_are_local_days_not_utc_days():
    """22:00 UTC is 03:30 next morning IST. The day it lands on is the whole
    point of pinning the timezone."""
    late = commit("WRITE", {"mode": "Overwrite"}, {}, f.at("2026-08-05", "22:00"))
    assert list(finish_times([late], date(2026, 8, 6), 30, UTC)) == [date(2026, 8, 5)]
    assert list(finish_times([late], date(2026, 8, 6), 30, IST)) == [date(2026, 8, 6)]


def test_commits_outside_the_window_are_excluded():
    old = commit("WRITE", {"mode": "Overwrite"}, {}, f.at("2026-06-01", "04:00"))
    assert finish_times([old], date(2026, 8, 6), 30, UTC) == {}


def test_summarize_carries_the_degraded_flag():
    commits = [commit("MERGE", {}, None, f.at("2026-08-06", "04:00"))]
    summary = summarize("c.s.t", commits, date(2026, 8, 6), 30, UTC)
    assert summary.metrics_degraded and len(summary.finish_times) == 1


# --- Tranche E: continuous detection -----------------------------------------

def test_looks_continuous_true_for_many_commits_per_day():
    from scheduler.history import looks_continuous
    # 10 qualifying commits each on 3 days
    commits = [commit("WRITE", {"mode": "Overwrite"}, {"numOutputRows": "1"},
                      f.at(f"2026-08-0{d}", f"{h:02d}:00"), version=d * 100 + h)
               for d in (1, 2, 3) for h in range(10)]
    assert looks_continuous(commits, date(2026, 8, 6), 30, UTC)


def test_looks_continuous_false_for_once_a_day():
    from scheduler.history import looks_continuous
    commits = [commit("WRITE", {"mode": "Overwrite"}, {"numOutputRows": "1"},
                      f.at(f"2026-08-0{d}", "04:00"), version=d) for d in (1, 2, 3)]
    assert not looks_continuous(commits, date(2026, 8, 6), 30, UTC)
