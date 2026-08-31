"""Steps 3 and 4 — roll up per-table finish times into two recommended clock times.

Pure arithmetic. Nothing here reads the wall clock or the network.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Sequence

from .models import TableHistory

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass(frozen=True)
class DependencyRow:
    """One line of the report."""
    name: str
    refreshes: str          # "daily", "Mondays", "Mon,Thu", ...
    usual: time | None      # median finish
    worst: time | None      # latest finish
    missed: int             # days in the common window it did not refresh
    degraded: bool = False


@dataclass(frozen=True)
class Recommendation:
    safe: time | None           # worst all-clear + buffer
    tighter: time | None        # second-worst all-clear + buffer
    n_days: int                 # days where every dependency refreshed
    span_days: int = 0          # observed span the `missed` counts are over
    rows: list[DependencyRow] = field(default_factory=list)
    all_clear: dict = field(default_factory=dict)   # date -> datetime


def _weekday_label(days: Sequence[date], span: Sequence[date]) -> str:
    """Describe which weekdays a table refreshes on, by observation.

    No classifier: this is the set of weekdays seen, said in words. A human
    reads "Mondays" faster than any cadence model decides it.
    """
    if not days:
        return "never"
    seen = sorted({d.weekday() for d in days})
    possible = sorted({d.weekday() for d in span})
    if set(seen) == set(possible) and len(days) >= len(span) * 0.9:
        return "daily"
    if len(seen) == 1:
        return WEEKDAYS[seen[0]] + "s"
    if len(seen) >= 6:
        return "daily"
    return ",".join(WEEKDAYS[d] for d in seen)


def build_rows(histories: Sequence[TableHistory], span: Sequence[date]) -> list[DependencyRow]:
    rows: list[DependencyRow] = []
    for history in histories:
        days = sorted(history.finish_times)
        times = [history.finish_times[d] for d in days]
        label = _weekday_label(days, span)
        # Only count a miss on days the table would have been expected to run.
        if label == "daily":
            expected = set(span)
        elif label == "never":
            expected = set()
        else:
            wanted = {d.weekday() for d in days}
            expected = {d for d in span if d.weekday() in wanted}
        rows.append(DependencyRow(
            name=history.name,
            refreshes=label,
            usual=_median_time(times),
            # max of the clock times, not of the datetimes: the latest moment
            # of day, which may not be on the latest day.
            worst=max((t.time() for t in times), default=None),
            missed=len(expected - set(days)),
            degraded=history.metrics_degraded,
        ))
    return rows


def _median_time(times: Sequence[datetime]) -> time | None:
    if not times:
        return None
    seconds = [t.hour * 3600 + t.minute * 60 + t.second for t in times]
    middle = int(statistics.median(seconds))
    return (datetime.min + timedelta(seconds=middle)).time()


def observed_span(histories: Sequence[TableHistory]) -> list[date]:
    """Every calendar day between the first and last refresh seen anywhere.

    Contiguous, not the set of days something actually refreshed on. Using the
    union of observed days makes a miss invisible whenever every dependency
    skipped the same day — and for a single dependency it makes `missed`
    permanently zero, because the only days in the span are the days it ran.
    """
    seen = sorted(set().union(*(set(h.finish_times) for h in histories)) or set())
    if not seen:
        return []
    return [seen[0] + timedelta(days=i) for i in range((seen[-1] - seen[0]).days + 1)]


def recommend(histories: Sequence[TableHistory], buffer_min: int) -> Recommendation:
    """The two numbers, plus the evidence behind them.

    A day counts only if EVERY dependency refreshed on it — you wait for the
    slowest table each day, not the average. Days where something did not run
    are excluded from the timing and surface in the missed column instead,
    because a table that misses days is a reliability problem no schedule fixes.
    """
    histories = list(histories)
    if not histories:
        return Recommendation(None, None, 0, 0, [], {})

    common = set(histories[0].finish_times)
    for history in histories[1:]:
        common &= set(history.finish_times)

    span = observed_span(histories)
    rows = build_rows(histories, span)

    if not common:
        return Recommendation(None, None, 0, len(span), rows, {})

    all_clear = {
        day: max(h.finish_times[day] for h in histories) for day in sorted(common)
    }
    # Order by clock time, not by datetime. The worst day is the one that
    # finished latest in the day, which is rarely the most recent date — sorting
    # the datetimes instead returns whatever happened last, which is a wrong
    # answer that looks entirely plausible.
    ordered = sorted(all_clear.values(), key=lambda m: m.time())
    safe = _plus(ordered[-1], buffer_min)
    tighter = _plus(ordered[-2], buffer_min) if len(ordered) >= 2 else safe
    return Recommendation(safe, tighter, len(common), len(span), rows, all_clear)


def _plus(moment: datetime, minutes: int) -> time:
    return (moment + timedelta(minutes=minutes)).time()
