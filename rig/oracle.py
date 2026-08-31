"""Turn the deterministic plan into the report the analyzer should produce.

The plan (`plan.expected_finishes`) is the independent source of truth: without
consulting the scheduler at all, it says which day each table refreshed and in
which slot. Feeding those finishes through the real `recommend()` gives the
reference Recommendation, and `mismatches()` compares the analyzer's actual
output against it — within a tolerance, because a real commit lands at its slot
plus however long a serverless cold start took.

There is deliberately no second implementation of the recommend arithmetic here.
An earlier version reimplemented it independently to get "differential" coverage;
that was a drift trap — the two copies had already diverged on sub-minute
rounding — and bought nothing over the plan finishes, which are the genuinely
independent signal. What no unit test can cover, and what real data stresses, is
whether the analyzer extracts those finishes correctly from real Delta history;
that is what this compares.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import plan
from scheduler.models import TableHistory
from scheduler.recommend import Recommendation, recommend

TZ = ZoneInfo("Asia/Kolkata")


def slot_time(slot: str) -> time:
    hour, minute = (int(part) for part in slot.split(":"))
    return time(hour, minute)


def finishes_upto(table: str, today: date, days_history: int) -> dict[date, str]:
    """The plan's finishes, clipped to what the analyzer would see today."""
    cutoff = today - timedelta(days=days_history)
    return {day: slot for day, slot in plan.expected_finishes(table).items()
            if cutoff < day <= today}


def expected_history(table: str, today: date, days_history: int) -> TableHistory:
    """A TableHistory as the analyzer should reconstruct it — finish times at
    exact slot precision (the real ones sit a cold-start later)."""
    finishes = finishes_upto(table, today, days_history)
    return TableHistory(
        name=table,
        finish_times={day: datetime.combine(day, slot_time(slot), tzinfo=TZ)
                      for day, slot in finishes.items()},
        metrics_degraded=False,
    )


def expected_recommendation(tables: list[str], today: date, buffer_min: int,
                            days_history: int) -> Recommendation:
    """The reference report: the real recommend() over the plan's finishes."""
    return recommend([expected_history(t, today, days_history) for t in tables],
                     buffer_min)


def _within(actual: time | None, expected_at: time | None, tolerance_min: int) -> bool:
    """actual must land in [expected, expected + tolerance]."""
    if actual is None or expected_at is None:
        return actual is expected_at
    base = datetime.combine(date(2000, 1, 1), expected_at)
    got = datetime.combine(date(2000, 1, 1), actual)
    return timedelta(0) <= got - base <= timedelta(minutes=tolerance_min)


def mismatches(expected: Recommendation, actual: Recommendation,
               qualified_names: dict[str, str] | None = None,
               tolerance_min: int = plan.SLOT_TOLERANCE_MIN) -> list[str]:
    """Every way the analyzer's report differs from the plan's reference.

    Day counts and category labels must match exactly; clock times may run up to
    tolerance_min later than the slot (serverless cold start). qualified_names
    maps the plan's short table names to the fully-qualified names the analyzer
    reports.
    """
    out: list[str] = []
    short = {v: k for k, v in (qualified_names or {}).items()}

    if expected.n_days != actual.n_days:
        out.append(f"n_days: expected {expected.n_days}, got {actual.n_days}")
    if not _within(actual.safe, expected.safe, tolerance_min):
        out.append(f"safe: expected {expected.safe} (+{tolerance_min}m), "
                   f"got {actual.safe}")
    if not _within(actual.tighter, expected.tighter, tolerance_min):
        out.append(f"tighter: expected {expected.tighter} (+{tolerance_min}m), "
                   f"got {actual.tighter}")

    by_name = {short.get(r.name, r.name): r for r in actual.rows}
    for row in expected.rows:
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
