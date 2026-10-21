"""Phase 2 gate, unit half. Every operation class, plus the two branches that
decide whether a live table reads as dormant."""

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest

from view_scheduler.client import parse_json_field
from view_scheduler.history import (
    classify, finish_times, is_degraded, qualifying, summarize,
)
from view_scheduler.models import Commit
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
        timestamp=datetime.fromtimestamp(int(row["ts_epoch"]), tz=UTC),
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


def test_the_first_commit_of_the_day_is_the_finish():
    """A day with two qualifying commits (04:00 staging, 05:00 final)
    reports 04:00. This is deliberate: a one-off rerun at 22:00 must not
    move the schedule. A job that commits twice per run needs a bigger
    buffer. See README, 'Which commit counts as the finish'."""
    day = date(2026, 8, 6)
    commits = [
        commit("WRITE", {"mode": "Overwrite"}, {}, f.at("2026-08-06", "04:00"), 1),
        commit("WRITE", {"mode": "Overwrite"}, {}, f.at("2026-08-06", "05:00"), 2),
        commit("WRITE", {"mode": "Overwrite"}, {}, f.at("2026-08-06", "22:00"), 3),
    ]
    times = finish_times(commits, day, 30, UTC)
    assert times[day] == f.at("2026-08-06", "04:00"), "the first commit is the finish"
    assert times[day].hour != 5, "the second commit does not move it"
    assert times[day].hour != 22, "a rerun at 22:00 does not move it"


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
    from view_scheduler.history import looks_continuous
    # 10 qualifying commits each on 3 days
    commits = [commit("WRITE", {"mode": "Overwrite"}, {"numOutputRows": "1"},
                      f.at(f"2026-08-0{d}", f"{h:02d}:00"), version=d * 100 + h)
               for d in (1, 2, 3) for h in range(10)]
    assert looks_continuous(commits, date(2026, 8, 6), 30, UTC)


def test_looks_continuous_false_for_once_a_day():
    from view_scheduler.history import looks_continuous
    commits = [commit("WRITE", {"mode": "Overwrite"}, {"numOutputRows": "1"},
                      f.at(f"2026-08-0{d}", "04:00"), version=d) for d in (1, 2, 3)]
    assert not looks_continuous(commits, date(2026, 8, 6), 30, UTC)


# --- unrecognised operations -------------------------------------------------

def test_an_unrecognised_operation_is_not_silently_treated_as_maintenance():
    """An operation in none of the three sets is 'unknown', not 'ignore'. Both
    verdicts decline to count it, but only one can be reported."""
    from view_scheduler.history import UNKNOWN_CLASS, classify
    assert classify(commit("REORG")) == UNKNOWN_CLASS
    assert classify(commit("OPTIMIZE")) == "ignore"
    assert not qualifying(commit("REORG")), "still conservative — it does not count"


def test_unknown_operations_are_collected_by_name():
    from view_scheduler.history import unknown_operations
    commits = [commit("REORG"), commit("CLUSTER BY"), commit("REORG"),
               commit("OPTIMIZE"), commit("WRITE", {"mode": "Append"})]
    assert unknown_operations(commits) == ("CLUSTER BY", "REORG")


def test_summarize_carries_unknown_operations():
    commits = [commit("REORG", {}, {}, f.at("2026-08-06", "03:00")),
               commit("WRITE", {"mode": "Overwrite"}, {}, f.at("2026-08-06", "04:00"))]
    summary = summarize("c.s.t", commits, date(2026, 8, 6), 30, UTC)
    assert summary.unknown_ops == ("REORG",)
    assert len(summary.finish_times) == 1, "the recognised write still counts"


def test_no_unknown_operations_on_a_clean_table():
    commits = [commit("WRITE", {"mode": "Overwrite"}, {}, f.at("2026-08-06", "04:00"))]
    assert summarize("c.s.t", commits, date(2026, 8, 6), 30, UTC).unknown_ops == ()


# --- multi-daily tables ------------------------------------------------------

def _write(day: int, hhmm: str, version: int) -> Commit:
    return commit("WRITE", {"mode": "Overwrite"}, {"numOutputRows": "1"},
                  f.at(f"2026-08-{day:02d}", hhmm), version=version)


def test_one_or_two_rerun_days_are_not_multi_daily():
    """A daily table with a couple of reruns still has median 1."""
    from view_scheduler.history import daily_slot_times, looks_multi_daily
    commits = []
    for day in range(1, 11):
        commits.append(_write(day, "04:00", day))
        if day in (2, 7):
            commits.append(_write(day, "16:00", 100 + day))
    today = date(2026, 8, 11)
    assert not looks_multi_daily(commits, today, 30, UTC)
    assert daily_slot_times(commits, today, 30, UTC) == ()


def test_three_writes_a_day_is_multi_daily_with_three_slots():
    from view_scheduler.history import (
        daily_slot_times, looks_continuous, looks_multi_daily,
    )
    commits = [
        _write(day, hhmm, day * 10 + i)
        for day in range(1, 11)
        for i, hhmm in enumerate(("02:00", "09:00", "17:00"))
    ]
    today = date(2026, 8, 11)
    assert looks_multi_daily(commits, today, 30, UTC)
    assert not looks_continuous(commits, today, 30, UTC)
    assert daily_slot_times(commits, today, 30, UTC) == (
        time(2, 0), time(9, 0), time(17, 0))


def test_summarize_carries_the_multi_daily_fields():
    commits = [
        _write(day, hhmm, day * 10 + i)
        for day in range(1, 11)
        for i, hhmm in enumerate(("02:00", "09:00", "17:00"))
    ]
    summary = summarize("c.s.t", commits, date(2026, 8, 11), 30, UTC)
    assert summary.multi_daily
    assert summary.daily_slots == (time(2, 0), time(9, 0), time(17, 0))
