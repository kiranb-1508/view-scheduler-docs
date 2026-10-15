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
    unknown_ops: tuple = ()         # operation names we could not classify
    multi_daily: bool = False       # refreshed 2+ times on a typical active day
    daily_slots: tuple = ()         # median finish times per daily run
    safe_slots: tuple = ()          # daily_slots plus the buffer


@dataclass(frozen=True)
class Recommendation:
    safe: time | None           # worst all-clear + buffer
    earlier: time | None        # usual time: median of the normal days + buffer
    n_days: int                 # days where every dependency refreshed
    span_days: int = 0          # observed span the `missed` counts are over
    rows: list[DependencyRow] = field(default_factory=list)
    all_clear: dict = field(default_factory=dict)   # date -> datetime
    # True when adding the buffer pushed the time past midnight, so the run
    # belongs to the following calendar day. Without this the clock time alone
    # reads ~24h earlier than meant.
    safe_next_day: bool = False
    earlier_next_day: bool = False
    safe_without_outliers: time | None = None
    safe_without_outliers_next_day: bool = False
    usual: time | None = None
    usual_next_day: bool = False
    outlier_days: tuple[date, ...] = ()


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


def dependency_rows(histories: Sequence[TableHistory], span: Sequence[date],
                    buffer_min: int = 0) -> list[DependencyRow]:
    """One report row per upstream table: its pattern, its usual, its worst."""
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
        safe_slots = tuple(
            (datetime.combine(date(2000, 1, 1), t)
             + timedelta(minutes=buffer_min)).time()
            for t in history.daily_slots
        )
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
            unknown_ops=history.unknown_ops,
            multi_daily=history.multi_daily,
            daily_slots=history.daily_slots,
            safe_slots=safe_slots,
        ))
    return rows


build_rows = dependency_rows


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


def _outlier_dates(finish_times: dict) -> tuple[date, ...]:
    """All dates in finish_times that stand out above the median.

    The scale is the mean absolute deviation. A small sample whose late days
    are a large share of the whole lifts that scale, so those days are not
    called outliers. Only a rare, clearly separated run flags.
    """
    if len(finish_times) < 4:
        return ()
    seconds = [_to_seconds(v.time()) for v in finish_times.values()]
    median = statistics.median(seconds)
    spread = statistics.mean([abs(s - median) for s in seconds])
    return tuple(sorted(
        day for day, dt in finish_times.items()
        if _to_seconds(dt.time()) - median > OUTLIER_FLOOR_MIN * 60
        and _to_seconds(dt.time()) - median > OUTLIER_MAD_K * spread
    ))


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
    """Compute safe/earlier times from the intersection of all dependencies' refresh days."""
    histories = list(histories)
    if not histories:
        return Recommendation(None, None, 0, 0, [], {})

    common = set(histories[0].finish_times)
    for history in histories[1:]:
        common &= set(history.finish_times)

    span = observed_span(histories)
    rows = dependency_rows(histories, span, buffer_min)

    if not common:
        return Recommendation(None, None, 0, len(span), rows, {})

    all_clear = {
        day: max(h.finish_times[day] for h in histories) for day in sorted(common)
    }
    # Sort by clock time, not datetime — latest time of day, not most recent date.
    ordered = sorted(all_clear.values(), key=lambda m: m.time())
    safe, safe_next = _add_buffer(ordered[-1], buffer_min)

    outliers = _outlier_dates(all_clear)
    normal_moments = (
        [dt for day, dt in all_clear.items() if day not in outliers]
        or list(all_clear.values())
    )
    ordered_normal = sorted(normal_moments, key=lambda m: m.time())
    safe_no_out, safe_no_out_next = _add_buffer(ordered_normal[-1], buffer_min)

    usual_clock = _median_time(normal_moments)
    usual, usual_next = _add_buffer(
        datetime.combine(ordered_normal[-1].date(), usual_clock), buffer_min)

    # `earlier` is the typical day's finish plus the buffer, in every case.
    # The outliers are reruns, not the usual pace, so they never move it.
    earlier, earlier_next = usual, usual_next
    return Recommendation(safe, earlier, len(common), len(span), rows, all_clear,
                          safe_next, earlier_next, safe_no_out, safe_no_out_next,
                          usual, usual_next, outliers)


def _add_buffer(moment: datetime, minutes: int) -> tuple[time, bool]:
    """Add the buffer, reporting whether it crossed midnight.

    The caller needs the rollover: a 23:50 all-clear plus a 15-minute buffer is
    00:05 *the next day*, and the bare clock time reads as a day earlier.
    """
    shifted = moment + timedelta(minutes=minutes)
    return shifted.time(), shifted.date() != moment.date()


_plus = _add_buffer
