"""Advise when a view can run, as an object you can read or log.

`advise()` is the easy front door to the analyzer. It prints a plain report and
returns an `Advice` that carries the same numbers as fields.

    from view_scheduler import advise

    advice = advise("sales.gold.customer_360")   # WAREHOUSE_ID from the env
    print(advice.safe_time)                      # 06:40   or None

`analyze()` is the low-level call. It returns a tuple of raw pieces. Use it
only when you need the per-table history objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time

from . import report
from .client import Workspace
from .config import DEFAULT, Config
from .models import Skipped
from .recommend import Recommendation
from .run import collect_history


@dataclass(frozen=True)
class DependencyRun:
    """One upstream job, and what its history looked like."""

    name: str
    runs: str                 # "daily", "Mondays", "never", ...
    usual: time | None        # its usual finish time
    latest: time | None       # its latest finish time
    days: int                 # days it ran in the period
    missed: int               # days it did not run
    note: str = ""
    multi_daily: bool = False
    daily_slots: tuple[time, ...] = ()
    safe_slots: tuple[time, ...] = ()


@dataclass(frozen=True)
class Advice:
    """When a view can safely run. One object, ready to read or log."""

    view: str
    safe_time: time | None
    earlier_time: time | None
    next_day: bool
    earlier_next_day: bool
    days_used: int
    period_days: int
    timezone: str
    dependencies: tuple[DependencyRun, ...] = ()
    unreadable: tuple[Skipped, ...] = ()
    finished_by_day: tuple[tuple[date, time], ...] = ()
    warnings: tuple[str, ...] = ()
    text: str = field(default="")
    safe_without_outliers: time | None = None
    usual_time: time | None = None
    outlier_days: tuple[date, ...] = ()

    def render(self) -> str:
        return self.text

    def to_dict(self) -> dict:
        """JSON-ready. Safe to log from a job."""
        return {
            "view": self.view,
            "safe_time": _clock(self.safe_time),
            "earlier_time": _clock(self.earlier_time),
            "next_day": self.next_day,
            "safe_without_outliers": _clock(self.safe_without_outliers),
            "usual_time": _clock(self.usual_time),
            "outlier_days": [day.isoformat() for day in self.outlier_days],
            "days_used": self.days_used,
            "period_days": self.period_days,
            "timezone": self.timezone,
            "dependencies": [
                {
                    "name": row.name,
                    "runs": row.runs,
                    "usual": _clock(row.usual),
                    "latest": _clock(row.latest),
                    "days": row.days,
                    "missed": row.missed,
                    "multi_daily": row.multi_daily,
                    "daily_slots": [_clock(t) for t in row.daily_slots],
                    "safe_slots": [_clock(t) for t in row.safe_slots],
                }
                for row in self.dependencies
            ],
            "unreadable": [
                {"name": item.name, "reason": item.reason}
                for item in self.unreadable
            ],
            "finished_by_day": [
                {"day": day.isoformat(), "time": moment.strftime("%H:%M")}
                for day, moment in self.finished_by_day
            ],
            "warnings": list(self.warnings),
        }

    def __str__(self) -> str:
        return self.text


def advise(
    view: str,
    ws: Workspace | None = None,
    config: Config | None = None,
    *,
    today: date | None = None,
    quiet: bool = False,
) -> Advice:
    """Suggest the time each day this view can run with every input ready.

    `ws` defaults to a Workspace built from the WAREHOUSE_ID environment
    variable. Pass `today` to make a test reproducible. `quiet=True` suppresses
    the printed report; the result is returned either way.
    """
    settings = config or DEFAULT
    workspace = ws or Workspace()
    moment = today or datetime.now(settings.tz).date()

    result, _, skipped = collect_history(view, workspace, settings, moment)
    advice = _to_advice(view, result, skipped, settings)

    if not quiet:
        print(advice)
    return advice


def _to_advice(view: str, result: Recommendation, skipped: list[Skipped],
               config: Config) -> Advice:
    """Turn the recommendation into the Advice object the caller returns."""
    rows = tuple(
        DependencyRun(
            name=row.name,
            runs=row.refreshes,
            usual=row.usual,
            latest=row.worst,
            days=row.observed_days,
            missed=row.missed,
            multi_daily=row.multi_daily,
            daily_slots=row.daily_slots,
            safe_slots=row.safe_slots,
        )
        for row in sorted(result.rows, key=lambda r: r.name)
    )
    earlier = (result.earlier
               if result.earlier and result.earlier != result.safe else None)
    finished = tuple(
        (day, moment)
        for day, moment in sorted(result.all_clear.items(),
                                  key=lambda kv: kv[1].time(), reverse=True)
    )
    return Advice(
        view=view,
        safe_time=result.safe,
        earlier_time=earlier,
        next_day=bool(result.safe and result.safe_next_day),
        earlier_next_day=bool(earlier and result.earlier_next_day),
        days_used=result.n_days,
        period_days=result.span_days,
        timezone=config.timezone,
        dependencies=rows,
        unreadable=tuple(skipped),
        finished_by_day=finished,
        warnings=tuple(report.warnings(result, skipped)),
        text=report.render(view, result, skipped, config.timezone),
        safe_without_outliers=result.safe_without_outliers,
        usual_time=result.usual,
        outlier_days=result.outlier_days,
    )


def _clock(value: time | None) -> str | None:
    return value.strftime("%H:%M") if value else None
