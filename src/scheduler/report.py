"""Step 4 — render the one screen the whole product is.

Warnings print only when they apply. No applicable warnings means the section is
absent entirely, not empty.
"""

from __future__ import annotations

from typing import Sequence

from .models import Skipped
from .recommend import Recommendation


def _hhmm(value) -> str:
    return value.strftime("%H:%M") if value else "—"


def _plural(n: int, word: str) -> str:
    if n == 1:
        return f"{n} {word}"
    if word.endswith("y") and word[-2] not in "aeiou":
        return f"{n} {word[:-1]}ies"
    return f"{n} {word}s"


def render(view: str, result: Recommendation, skipped: Sequence[Skipped],
           timezone: str) -> str:
    lines: list[str] = []
    deps = len(result.rows)
    lines.append(f"VIEW   {view}")
    lines.append(f"       {_plural(deps, 'dependency')}"
                 f" · {_plural(result.n_days, 'day')} history · times {timezone}")
    lines.append("")

    if result.safe:
        lines.append(f"       START AT {_hhmm(result.safe)}   safe — later than "
                     f"all {_plural(result.n_days, 'day')}")
        if result.tighter and result.tighter != result.safe:
            lines.append(f"       or       {_hhmm(result.tighter)}   tighter — "
                         f"would have been early on 1 day in {result.n_days}")
    else:
        lines.append("       NO RECOMMENDATION — no day in the window had every "
                     "dependency refresh.")
        lines.append("       The rows below show which ones did not.")
    lines.append("")

    if result.rows:
        # Size the name column to the longest name. Fully-qualified names run
        # long, and a fixed width silently welds the name to the next column.
        width = max([len(r.name) for r in result.rows] + [len("DEPENDENCY")]) + 2
        # Size REFRESHES too: a multi-weekday label like "Tue,Wed,Thu,Fri" is
        # wider than a fixed column and would weld to the next one.
        rw = max([len(r.refreshes) for r in result.rows] + [len("REFRESHES")]) + 2
        # DAYS = days this dependency actually refreshed in the window. A number
        # far below the others is what shrank the all-clear count, so it belongs
        # on screen next to the timing.
        lines.append(f"{'DEPENDENCY':<{width}}{'REFRESHES':<{rw}}{'USUAL':<8}"
                     f"{'WORST':<8}{'DAYS':<6}{'MISSED'}")
        for row in sorted(result.rows, key=lambda r: r.name):
            lines.append(
                f"{row.name:<{width}}{row.refreshes:<{rw}}{_hhmm(row.usual):<8}"
                f"{_hhmm(row.worst):<8}{row.observed_days:<6}{row.missed}")

    warnings = _warnings(result, skipped)
    if warnings:
        lines.append("")
        lines.extend(warnings)
    return "\n".join(lines)


def _warnings(result: Recommendation, skipped: Sequence[Skipped]) -> list[str]:
    """Only what applies to the view in front of you."""
    out: list[str] = []

    for row in sorted(result.rows, key=lambda r: r.name):
        if row.refreshes not in ("daily", "never") and row.usual:
            out.append(f"!  {row.name} refreshes {row.refreshes} only → it will "
                       f"not be fresh on other days")
    for row in sorted(result.rows, key=lambda r: r.name):
        if row.missed:
            out.append(f"!  {row.name} missed {row.missed} of "
                       f"{result.span_days} observed days "
                       f"— no schedule time fixes that")
    # A late outlier day pins `safe` for the whole month; name it so the reader
    # knows `tighter` is the truer number. (E2)
    for row in sorted(result.rows, key=lambda r: r.name):
        if row.outlier_day:
            out.append(f"!  {row.name} was unusually late on "
                       f"{row.outlier_day.isoformat()} ({_hhmm(row.worst)} vs "
                       f"usual {_hhmm(row.usual)}) — that one day sets the safe "
                       f"time; without it, tighter applies")
    # Continuously-refreshed tables have a meaningless "first commit of day"
    # finish that runs early — the recommendation reads it as ready too soon. (E3)
    for row in sorted(result.rows, key=lambda r: r.name):
        if row.continuous:
            out.append(f"!  {row.name} refreshes many times a day — its time is a "
                       f"floor, not when it is actually caught up; verify before "
                       f"trusting an early start")
    for row in sorted(result.rows, key=lambda r: r.name):
        if row.degraded:
            out.append(f"!  {row.name} writes without row metrics — its commits "
                       f"are counted but not verified, so a skipped day cannot be "
                       f"detected for it")
    for row in sorted(result.rows, key=lambda r: r.name):
        if row.refreshes == "never":
            out.append(f"!  {row.name} has no qualifying refresh in the window "
                       f"— it may be dormant, or written by a path we cannot read")

    if skipped:
        out.append(f"!  {len(skipped)} dependenc"
                   f"{'y' if len(skipped) == 1 else 'ies'} skipped:")
        for item in skipped:
            out.append(f"     {item.name} — {item.reason}")

    if result.n_days and result.n_days < 10:
        starved = min((r for r in result.rows), key=lambda r: r.observed_days,
                      default=None)
        culprit = (f" — {starved.name} has only "
                   f"{_plural(starved.observed_days, 'day')}"
                   if starved and starved.observed_days <= result.n_days else "")
        out.append(f"!  only {_plural(result.n_days, 'complete day')} where every "
                   f"dependency refreshed{culprit}; the recommendation is weakly "
                   f"evidenced")
    return out
