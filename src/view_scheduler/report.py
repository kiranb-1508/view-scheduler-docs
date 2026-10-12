"""Render the advice as text. Warnings print only when they apply.

Plain words only. No jargon. A reader should know what to do after one read.
"""

from __future__ import annotations

from typing import Sequence

from .models import Skipped
from .recommend import WEEKDAYS, Recommendation


def _hhmm(value) -> str:
    return value.strftime("%H:%M") if value else "—"


def _day_mark(next_day: bool) -> str:
    return " (+1 day)" if next_day else ""


def _plural(n: int, word: str) -> str:
    if n == 1:
        return f"{n} {word}"
    if word.endswith("y") and word[-2] not in "aeiou":
        return f"{n} {word[:-1]}ies"
    return f"{n} {word}s"


def _too_early(result: Recommendation) -> int:
    """How many observed days would have been too early at the earlier time."""
    if not result.earlier:
        return 0
    return sum(1 for moment in result.all_clear.values()
               if moment.time() > result.earlier)


def render(view: str, result: Recommendation, skipped: Sequence[Skipped],
           timezone: str) -> str:
    """The advice report: the two times, the evidence, and the warnings.

    Plain words only. A reader must know what to do after one read.
    """
    lines: list[str] = []
    deps = len(result.rows)
    lines.append(f"VIEW   {view}")
    lines.append(f"       {_plural(deps, 'dependency')} · last "
                 f"{_plural(result.span_days, 'day')} · times {timezone}")
    lines.append("")

    if result.safe:
        lines.append(
            f"       SCHEDULE AT  {_hhmm(result.safe)}"
            f"{_day_mark(result.safe_next_day)}"
            f"    safe — every job had finished, on all "
            f"{_plural(result.n_days, 'day')}")
        if result.earlier and result.earlier != result.safe:
            early = _too_early(result)
            note = (f"one job had not finished yet, on {early} of "
                    f"{_plural(result.n_days, 'day')}"
                    if early else "safe too, but with less room to spare")
            lines.append(f"       EARLIER      {_hhmm(result.earlier)}"
                         f"{_day_mark(result.earlier_next_day)}    {note}")
    else:
        lines.append("       NO TIME TO SUGGEST — there is no day where every "
                     "job had finished.")
        lines.append("       The table below shows which jobs did not run.")
    lines.append("")

    if result.rows:
        width = max([len(r.name) for r in result.rows] + [len("DEPENDENCY")]) + 2
        rw = max([len(r.refreshes) for r in result.rows] + [len("RUNS")]) + 2
        lines.append(f"{'DEPENDENCY':<{width}}{'RUNS':<{rw}}{'USUAL':<8}"
                     f"{'LATEST':<8}{'DAYS':<6}{'MISSED'}")
        for row in sorted(result.rows, key=lambda r: r.name):
            lines.append(
                f"{row.name:<{width}}{row.refreshes:<{rw}}{_hhmm(row.usual):<8}"
                f"{_hhmm(row.worst):<8}{row.observed_days:<6}{row.missed}")

    series = all_clear_series(result)
    if series:
        lines.append("")
        lines.extend(series)

    notes = warnings(result, skipped)
    if notes:
        lines.append("")
        lines.extend(notes)
    return "\n".join(lines)


def all_clear_series(result: Recommendation) -> list[str]:
    """Which day set each of the two times. This is the evidence."""
    if not result.all_clear:
        return []
    ordered = sorted(result.all_clear.items(), key=lambda kv: kv[1].time(),
                     reverse=True)
    out = ["WHEN THE LAST JOB FINISHED, DAY BY DAY   (latest first)"]
    for i, (day, moment) in enumerate(ordered):
        if i == 0:
            mark = "   ← sets the safe time"
        elif i == 1:
            mark = "   ← sets the earlier time"
        else:
            mark = ""
        out.append(f"  {day.isoformat()}  {WEEKDAYS[day.weekday()]}  "
                   f"{moment.strftime('%H:%M')}{mark}")
    return out


def warnings(result: Recommendation, skipped: Sequence[Skipped]) -> list[str]:
    """Every reason to distrust the two times, one line each.

    A warning never changes a number. It tells the reader what the number
    cannot say.
    """
    out: list[str] = []

    for row in sorted(result.rows, key=lambda r: r.name):
        if row.refreshes not in ("daily", "never") and row.usual:
            out.append(f"!  {row.name} runs only on {row.refreshes} — it will "
                       f"not be fresh on other days")
        if row.missed:
            out.append(f"!  {row.name} did not run on {row.missed} of "
                       f"{result.span_days} days — no schedule time fixes that")
        if row.outlier_day:
            out.append(f"!  {row.name} finished very late on "
                       f"{row.outlier_day.isoformat()} ({_hhmm(row.worst)}, "
                       f"usually {_hhmm(row.usual)}) — that one day sets the "
                       f"safe time. Without it you could use "
                       f"{_hhmm(result.earlier)}.")
        if row.continuous:
            out.append(f"!  {row.name} runs many times a day — the time above "
                       f"is only its first run, not when it is fully caught "
                       f"up. Check before using an early start.")
        if row.degraded:
            out.append(f"!  {row.name} writes no row counts — we can see it "
                       f"ran, but not that it wrote data. A missed day cannot "
                       f"be spotted for it.")
        if row.refreshes == "never":
            out.append(f"!  {row.name} has not run in this period — it may be "
                       f"stopped, or written by something we cannot read")
        if row.unknown_ops:
            out.append(f"!  {row.name} has writes we did not recognise "
                       f"({', '.join(row.unknown_ops)}). We did not count "
                       f"them, so its real time may be later than shown.")

    if result.safe_next_day:
        out.append(f"!  {_hhmm(result.safe)} is on the NEXT day. It is the "
                   f"safe time plus the gap you asked for. If you schedule it "
                   f"as a same-day time, it runs before that day's jobs, on "
                   f"yesterday's data.")

    if skipped:
        word = "dependency" if len(skipped) == 1 else "dependencies"
        out.append(f"!  {len(skipped)} {word} could not be read:")
        for item in skipped:
            out.append(f"     {item.name} — {item.reason}")

    if result.n_days and result.n_days < 10:
        starved = min((r for r in result.rows), key=lambda r: r.observed_days,
                      default=None)
        culprit = (f" ({starved.name} ran on only "
                   f"{_plural(starved.observed_days, 'day')})"
                   if starved and starved.observed_days <= result.n_days else "")
        out.append(f"!  only {_plural(result.n_days, 'day')} had every job run"
                   f"{culprit} — this advice is weak")
    return out
