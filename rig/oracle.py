"""What the analyzer should say, computed from the plan rather than from the code.

The rig writes deterministically, so the correct report is knowable in advance.
This module derives it, and `mismatches()` compares it against what the analyzer
actually produced.

Timing tolerance: a commit lands at its slot plus however long a serverless cold
start took. An expected time is therefore a window — [slot, slot + tolerance] —
not an instant.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import plan

TZ = ZoneInfo("Asia/Kolkata")


def slot_time(slot: str) -> time:
    hour, minute = (int(part) for part in slot.split(":"))
    return time(hour, minute)


def _at(day: date, slot: str) -> datetime:
    return datetime.combine(day, slot_time(slot), tzinfo=TZ)


@dataclass(frozen=True)
class ExpectedRow:
    name: str
    refreshes: str
    usual: time
    worst: time
    missed: int


@dataclass(frozen=True)
class Expected:
    safe: time | None
    tighter: time | None
    n_days: int
    rows: list[ExpectedRow] = field(default_factory=list)


def finishes_upto(table: str, today: date, days_history: int) -> dict[date, str]:
    """The plan's finishes, clipped to what the analyzer would see today."""
    cutoff = today - timedelta(days=days_history)
    return {day: slot for day, slot in plan.expected_finishes(table).items()
            if cutoff < day <= today}


def _label(days: list[date], span: list[date]) -> str:
    """Mirrors scheduler.recommend._weekday_label, stated independently."""
    if not days:
        return "never"
    seen = sorted({d.weekday() for d in days})
    possible = sorted({d.weekday() for d in span})
    if set(seen) == set(possible) and len(days) >= len(span) * 0.9:
        return "daily"
    if len(seen) == 1:
        return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][seen[0]] + "s"
    if len(seen) >= 6:
        return "daily"
    return ",".join(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][d] for d in seen)


def expected(tables: list[str], today: date, buffer_min: int = 15,
             days_history: int = 30) -> Expected:
    per_table = {t: finishes_upto(t, today, days_history) for t in tables}

    seen = sorted(set().union(*(set(v) for v in per_table.values())) or set())
    # Contiguous calendar days, matching scheduler.recommend.observed_span.
    span = ([seen[0] + timedelta(days=i)
             for i in range((seen[-1] - seen[0]).days + 1)] if seen else [])
    rows: list[ExpectedRow] = []
    for table, finishes in per_table.items():
        days = sorted(finishes)
        times = [slot_time(finishes[d]) for d in days]
        label = _label(days, span)
        if label == "daily":
            due = set(span)
        elif label == "never":
            due = set()
        else:
            wanted = {d.weekday() for d in days}
            due = {d for d in span if d.weekday() in wanted}
        rows.append(ExpectedRow(
            name=table,
            refreshes=label,
            usual=_median(times),
            worst=max(times) if times else None,
            missed=len(due - set(days)),
        ))

    common = set(per_table[tables[0]]) if tables else set()
    for table in tables[1:]:
        common &= set(per_table[table])
    if not common:
        return Expected(None, None, 0, rows)

    all_clear = sorted(
        (max(slot_time(per_table[t][day]) for t in tables) for day in common))
    return Expected(
        safe=_plus(all_clear[-1], buffer_min),
        tighter=_plus(all_clear[-2] if len(all_clear) >= 2 else all_clear[-1],
                      buffer_min),
        n_days=len(common),
        rows=rows,
    )


def _median(times: list[time]) -> time | None:
    if not times:
        return None
    seconds = [t.hour * 3600 + t.minute * 60 for t in times]
    return (datetime.min + timedelta(seconds=int(statistics.median(seconds)))).time()


def _plus(moment: time, minutes: int) -> time:
    return (datetime.combine(date(2000, 1, 1), moment)
            + timedelta(minutes=minutes)).time()


def _within(actual: time | None, expected_at: time | None, tolerance_min: int) -> bool:
    """actual must land in [expected, expected + tolerance]."""
    if actual is None or expected_at is None:
        return actual is expected_at
    base = datetime.combine(date(2000, 1, 1), expected_at)
    got = datetime.combine(date(2000, 1, 1), actual)
    return timedelta(0) <= got - base <= timedelta(minutes=tolerance_min)


def mismatches(exp: Expected, actual, qualified_names: dict[str, str] | None = None,
               tolerance_min: int = plan.SLOT_TOLERANCE_MIN) -> list[str]:
    """Every way the analyzer's output differs from what the plan predicts.

    qualified_names maps the plan's short table names to the fully-qualified
    names the analyzer reports.
    """
    out: list[str] = []
    short = {v: k for k, v in (qualified_names or {}).items()}

    if exp.n_days != actual.n_days:
        out.append(f"n_days: expected {exp.n_days}, got {actual.n_days}")
    if not _within(actual.safe, exp.safe, tolerance_min):
        out.append(f"safe: expected {exp.safe} (+{tolerance_min}m), got {actual.safe}")
    if not _within(actual.tighter, exp.tighter, tolerance_min):
        out.append(f"tighter: expected {exp.tighter} (+{tolerance_min}m), "
                   f"got {actual.tighter}")

    by_name = {short.get(r.name, r.name): r for r in actual.rows}
    for row in exp.rows:
        got = by_name.get(row.name)
        if got is None:
            out.append(f"{row.name}: missing from the report")
            continue
        if got.refreshes != row.refreshes:
            out.append(f"{row.name}.refreshes: expected {row.refreshes}, "
                       f"got {got.refreshes}")
        if got.missed != row.missed:
            out.append(f"{row.name}.missed: expected {row.missed}, got {got.missed}")
        if not _within(got.usual, row.usual, tolerance_min):
            out.append(f"{row.name}.usual: expected {row.usual}, got {got.usual}")
        if not _within(got.worst, row.worst, tolerance_min):
            out.append(f"{row.name}.worst: expected {row.worst}, got {got.worst}")
    return out
