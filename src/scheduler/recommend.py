"""Roll up per-table finish times into two recommended clock times. Pure arithmetic."""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Sequence

from .models import TableHistory

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


#: The worst day is called an outlier when it sits this far above the table's
#: usual finish AND beyond this many robust deviations (MAD) of its own spread.
OUTLIER_FLOOR_MIN = 30
OUTLIER_MAD_K = 3


@dataclass(frozen=True)
class DependencyRow:
    """One line of the report."""
    name: str
    refreshes: str          # "daily", "Mondays", "Mon,Thu", ...
    usual: time | None      # median finish
    worst: time | None      # latest finish
    missed: int             # days in the common window it did not refresh
    observed_days: int = 0  # days it actually refreshed in the window
    degraded: bool = False
    continuous: bool = False        # refreshed many times a day; finish is a floor
    outlier_day: date | None = None # the day that pins `worst`, if it is an outlier


@dataclass(frozen=True)
class Recommendation:
    safe: time | None           # worst all-clear + buffer
    tighter: time | None        # second-worst all-clear + buffer
    n_days: int                 # days where every dependency refreshed
    span_days: int = 0          # observed span the `missed` counts are over
    rows: list[DependencyRow] = field(default_factory=list)
    all_clear: dict = field(default_factory=dict)   # date -> datetime


def _weekday_label(days: Sequence[date], span: Sequence[date]) -> str:
    """'daily', 'Mondays', 'Mon,Thu', etc. — observed weekday pattern in words."""
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
        usual = _median_time(times)
        # max of the clock times, not of the datetimes: the latest moment of
        # day, which may not be on the latest day.
        worst = max((t.time() for t in times), default=None)
        rows.append(DependencyRow(
            name=history.name,
            refreshes=label,
            usual=usual,
            worst=worst,
            missed=len(expected - set(days)),
            observed_days=len(days),
            degraded=history.metrics_degraded,
            continuous=history.continuous,
            outlier_day=_outlier_day(history.finish_times, usual, worst),
        ))
    return rows


def _to_seconds(t) -> int:
    return t.hour * 3600 + t.minute * 60 + t.second


def _outlier_day(finish_times: dict, usual: time | None,
                 worst: time | None) -> date | None:
    """The date that pins `worst`, if it stands out by MAD from the table's own spread."""
    if usual is None or worst is None or len(finish_times) < 4:
        return None
    seconds = [_to_seconds(v.time()) for v in finish_times.values()]
    median = statistics.median(seconds)
    mad = statistics.median([abs(s - median) for s in seconds])
    worst_s = _to_seconds(worst)
    usual_s = _to_seconds(usual)
    if worst_s - usual_s <= OUTLIER_FLOOR_MIN * 60:
        return None
    if worst_s - median <= OUTLIER_MAD_K * mad:
        return None
    latest = max(finish_times.values(), key=lambda v: v.time())
    return latest.date()


def _median_time(times: Sequence[datetime]) -> time | None:
    if not times:
        return None
    seconds = [_to_seconds(t) for t in times]
    middle = int(statistics.median(seconds))
    return (datetime.min + timedelta(seconds=middle)).time()


def observed_span(histories: Sequence[TableHistory]) -> list[date]:
    """Contiguous date range from the earliest to latest refresh seen anywhere."""
    seen = sorted(set().union(*(set(h.finish_times) for h in histories)) or set())
    if not seen:
        return []
    return [seen[0] + timedelta(days=i) for i in range((seen[-1] - seen[0]).days + 1)]


def recommend(histories: Sequence[TableHistory], buffer_min: int) -> Recommendation:
    """Compute safe/tighter times from the intersection of all dependencies' refresh days."""
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
    # Sort by clock time, not datetime — latest time of day, not most recent date.
    ordered = sorted(all_clear.values(), key=lambda m: m.time())
    safe = _plus(ordered[-1], buffer_min)
    tighter = _plus(ordered[-2], buffer_min) if len(ordered) >= 2 else safe
    return Recommendation(safe, tighter, len(common), len(span), rows, all_clear)


def _plus(moment: datetime, minutes: int) -> time:
    return (moment + timedelta(minutes=minutes)).time()
