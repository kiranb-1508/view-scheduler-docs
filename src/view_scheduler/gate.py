"""Gate: can a view refresh now?

Give it a view, or a list of views. It walks each one down to the physical
tables it reads and checks that every one of them wrote recently.

    READY    every dependency is fresh. The refresh can proceed.
    BLOCKED  something is stale, missing, or unreadable. The report names the
             table and the view that needs it.

READ ONLY, and DECIDE ONLY. The gate never refreshes anything. It answers one
question, and the calling job acts on the answer.

A dependency is fresh when it has a qualifying write within the last
`Config.fresh_hours` hours (default 24). A streaming table is checked like
any table: it is a Delta table, its history is readable, and one on a
schedule is stale between runs. A materialized view is read from its pipeline
event log. Anything we cannot read blocks the view, because the gate fails
closed.

The call can relax the daily check. `ignore_tables` names tables to skip.
`ignore_non_daily` checks a weekly or static table against its own schedule.
`check_multi_daily` shortens the window for a table that runs many times a
day. A `view_scheduler.mode` table property can mark one table as static or
event driven.

    from view_scheduler import can_refresh

    result = can_refresh("sales.gold.customer_360")   # WAREHOUSE_ID from env
    if result.ready:
        ...run the refresh...
    else:
        print(result)
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Sequence
from zoneinfo import ZoneInfo

from .client import Workspace, failure_category, read_commits
from .config import DEFAULT, Config
from .history import finish_times, qualifying, qualifying_per_day
from .recommend import _weekday_label
from .walker import walk

#: Values of the `view_scheduler.mode` table property that mean "do not gate on
#: this table". The gate reads the property from the Unity Catalog metadata.
IGNORED_MODES = {
    "event_driven", "event-driven", "on_demand", "on-demand", "static", "ignore",
}


@dataclass(frozen=True)
class Dependency:
    """One thing a view waits for, and whether it is ready."""

    name: str
    kind: str
    ok: bool
    last_refresh: datetime | None = None
    age_minutes: int | None = None
    detail: str = ""
    cadence: str = ""
    multi_daily: bool = False


@dataclass(frozen=True)
class ViewResult:
    """The gate verdict for one view."""

    view: str
    ready: bool
    dependencies: tuple[Dependency, ...] = ()

    def blockers(self) -> tuple[str, ...]:
        """Names of the dependencies that hold this view up."""
        return tuple(dep.name for dep in self.dependencies if not dep.ok)


@dataclass(frozen=True)
class GateResult:
    """The gate verdict for a whole call."""

    ready: bool
    views: tuple[ViewResult, ...]
    fresh_hours: int
    timezone: str

    def blockers(self) -> tuple[str, ...]:
        """Every dependency holding anything up, in order, without repeats."""
        found: list[str] = []
        for view in self.views:
            for name in view.blockers():
                if name not in found:
                    found.append(name)
        return tuple(found)

    def to_dict(self) -> dict:
        """JSON-ready. Safe to log from a job."""
        return {
            "ready": self.ready,
            "fresh_hours": self.fresh_hours,
            "timezone": self.timezone,
            "blockers": list(self.blockers()),
            "views": [
                {
                    "view": view.view,
                    "ready": view.ready,
                    "dependencies": [
                        {
                            "name": dep.name,
                            "kind": dep.kind,
                            "ok": dep.ok,
                            "last_refresh": (dep.last_refresh.isoformat()
                                             if dep.last_refresh else None),
                            "age_minutes": dep.age_minutes,
                            "detail": dep.detail,
                            "cadence": dep.cadence,
                            "multi_daily": dep.multi_daily,
                        }
                        for dep in view.dependencies
                    ],
                }
                for view in self.views
            ],
        }

    def render(self) -> str:
        """The report a person reads. One block per view, then what to do."""
        zone = ZoneInfo(self.timezone)
        verdict = "READY" if self.ready else "BLOCKED"
        lines = [
            f"GATE  {verdict}   {_plural(len(self.views), 'view')}, "
            f"window {self.fresh_hours}h, times {self.timezone}",
            "",
        ]
        for view in self.views:
            label = "READY" if view.ready else "BLOCKED"
            lines.append(f"  {label:<8}{view.view}")
            if not view.dependencies:
                lines.append("      note   no dependencies to wait for")
            for dep in sorted(view.dependencies, key=lambda d: (d.ok, d.name)):
                lines.append("      " + _dep_line(dep, zone))
            lines.append("")
        if self.ready:
            lines.append("Every dependency is fresh. The refresh can proceed.")
        else:
            lines.append("To unblock, refresh:")
            for view in self.views:
                for dep in view.dependencies:
                    if not dep.ok:
                        lines.append(f"  {dep.name}   (needed by {view.view})")
        return "\n".join(lines)

    def __str__(self) -> str:
        return self.render()


def can_refresh(
    views: str | Sequence[str],
    ws: Workspace | None = None,
    config: Config | None = None,
    *,
    now: datetime | None = None,
    fresh_hours: int | None = None,
    ignore_non_daily: bool = False,
    ignore_tables: Sequence[str] = (),
    check_multi_daily: bool = False,
    quiet: bool = False,
) -> GateResult:
    """Report whether every dependency of each view wrote recently enough.

    `views` is one view name or a list of them. `ws` defaults to a Workspace
    built from the WAREHOUSE_ID environment variable, so the common one-liner
    `can_refresh("catalog.schema.my_view")` works inside a Databricks job.
    `fresh_hours` overrides `Config.fresh_hours` for this call only. Pass `now`
    to make a test reproducible. `quiet=True` suppresses the printed report; the
    result is returned either way.

    Three options relax the default daily check:

    `ignore_tables` names tables the caller does not want to gate on. A table in
    this list passes, whatever its history shows.

    `ignore_non_daily` passes a table that does not refresh every day. The gate
    checks it against its own schedule, up to a 168-hour window, and passes a
    table with no refresh in history as a note.

    `check_multi_daily` shortens the window for a table that runs many times a
    day. The window becomes `fresh_hours // (runs per day)`, so a table with
    three runs a day must have written within a third of the window.
    """
    names = [views] if isinstance(views, str) else [str(name) for name in views]
    if not names:
        raise ValueError("can_refresh needs at least one view name")

    settings = config or DEFAULT
    window = settings.fresh_hours if fresh_hours is None else fresh_hours
    if window <= 0:
        raise ValueError(f"fresh_hours must be positive, got {window}")

    moment = now or datetime.now(settings.tz)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=settings.tz)
    cutoff = moment - timedelta(hours=window)
    not_after = int(moment.timestamp())

    workspace = ws or Workspace()
    ignored_set = {str(name) for name in ignore_tables}

    # Shared across the whole call: a table two views depend on is read once,
    # and a failure is remembered rather than retried for every view.
    meta_cache: dict[str, dict | Exception] = {}
    hist_cache: dict[str, tuple[datetime | None, str, bool, int] | Exception] = {}

    def fetch_meta(name: str) -> dict:
        if name not in meta_cache:
            try:
                meta_cache[name] = workspace.get_table(name)
            except Exception as exc:
                meta_cache[name] = exc
        cached = meta_cache[name]
        if isinstance(cached, Exception):
            raise cached
        return cached

    def mode_of(name: str) -> str:
        """The `view_scheduler.mode` table property, lowercased, or ''."""
        meta = meta_cache.get(name)
        if not isinstance(meta, dict):
            return ""
        props = meta.get("properties") or {}
        if not isinstance(props, dict):
            return ""
        return str(props.get("view_scheduler.mode")
                   or props.get("view_scheduler.cadence") or "").strip().lower()

    def history_of(name: str):
        if name not in hist_cache:
            try:
                hist_cache[name] = _history_summary(
                    workspace, name, moment, settings, not_after)
            except Exception as exc:
                hist_cache[name] = exc
        return hist_cache[name]

    results: list[ViewResult] = []
    for view in names:
        found = walk(view, fetch_meta)
        deps = [_verdict_for(ref, history_of(ref.name), cutoff, moment, window,
                             ignored_set=ignored_set, mode=mode_of(ref.name),
                             ignore_non_daily=ignore_non_daily,
                             check_multi_daily=check_multi_daily)
                for ref in found.tables]
        deps += [Dependency(item.name, "UNKNOWN", False, detail=item.reason)
                 for item in found.blockers]
        deps += [Dependency(item.name, "FUNCTION", True, detail=item.reason)
                 for item in found.notes]
        results.append(ViewResult(view, all(dep.ok for dep in deps), tuple(deps)))

    outcome = GateResult(
        ready=all(view.ready for view in results),
        views=tuple(results),
        fresh_hours=window,
        timezone=settings.timezone,
    )
    if not quiet:
        print(outcome)
    return outcome


def _history_summary(workspace: Workspace, name: str, moment: datetime,
                     settings: Config, not_after: int
                     ) -> tuple[datetime | None, str, bool, int]:
    """(newest qualifying write, cadence label, multi-daily, runs per day).

    `latest` ignores a write dated after `not_after`, so a clock-skewed future
    commit cannot make a stale table look fresh. The cadence label comes from
    the weekdays the table wrote on in the window. `multi_daily` is True when
    the median active day held two or more qualifying commits.

    The read is bounded in SQL to the start of the local day `days_history`
    days before `moment`, exactly as `run.collect_history` computes it. The
    cadence is computed over that many days, so the gate never needs older
    commits, and the warehouse returns a small window instead of the whole
    history of a long-lived table.
    """
    cutoff_day = moment.date() - timedelta(days=settings.days_history)
    since_epoch = int(datetime.combine(cutoff_day, time.min,
                                       tzinfo=settings.tz).timestamp())
    commits = read_commits(workspace, name, with_metrics=False,
                           since_epoch=since_epoch)
    latest: datetime | None = None
    for commit in commits:
        if not qualifying(commit):
            continue
        if commit.timestamp.timestamp() > not_after:
            continue
        if latest is None or commit.timestamp > latest:
            latest = commit.timestamp

    days = sorted(finish_times(commits, moment.date(),
                               settings.days_history, settings.tz))
    span = [moment.date() - timedelta(days=i)
            for i in range(settings.days_history - 1, -1, -1)]
    cadence = _weekday_label(days, span)
    per_day = qualifying_per_day(commits, moment.date(),
                                 settings.days_history, settings.tz)
    median_runs = int(statistics.median(per_day.values())) if per_day else 0
    return latest, cadence, median_runs >= 2, median_runs


def _verdict_for(ref, history, cutoff: datetime, moment: datetime, window: int, *,
                 ignored_set: set[str], mode: str, ignore_non_daily: bool,
                 check_multi_daily: bool) -> Dependency:
    """Turn one dependency's history into a fresh/stale verdict.

    The default check is one rule: a qualifying write within `window` hours.
    The options relax it. The caller can ignore a table by name. A table can
    mark itself with a `view_scheduler.mode` property. A weekly or static table
    is checked against its real schedule. A table that runs many times a day is
    checked against a shorter window.

    A streaming table goes through the same check as a managed table. A
    streaming table on a schedule commits once per run, so between runs it is
    stale, and a paused one is stale the whole time it is paused. Calling it
    fresh by definition is a false READY — the failure this gate exists to
    prevent.
    """
    # An unreadable history fails closed. The gate cannot confirm the write, so
    # it blocks. This holds even for a table that is marked as ignored.
    if isinstance(history, Exception):
        return Dependency(ref.name, ref.kind, False,
                          detail=f"history unreadable ({failure_category(history)})")
    latest, cadence, multi_daily, median_runs = history
    minutes = (None if latest is None
               else max(0, int((moment - latest).total_seconds() // 60)))

    # The caller, or the table itself, said "do not gate on this".
    if ref.name in ignored_set:
        return Dependency(ref.name, ref.kind, True, latest, minutes,
                          detail="ignored by caller", cadence=cadence,
                          multi_daily=multi_daily)
    if mode in IGNORED_MODES:
        return Dependency(ref.name, ref.kind, True, latest, minutes,
                          detail=f"{mode} table — ignored", cadence=cadence,
                          multi_daily=multi_daily)

    # No qualifying refresh in history.
    if latest is None:
        if ignore_non_daily:
            return Dependency(ref.name, ref.kind, True, None, None,
                              detail="static table (no refresh in history) — ignored",
                              cadence="never", multi_daily=False)
        return Dependency(ref.name, ref.kind, False, None, None,
                          detail="no qualifying refresh in history",
                          cadence=cadence, multi_daily=multi_daily)

    # A table that runs many times a day needs a shorter window.
    if check_multi_daily and multi_daily:
        eff_window = max(1, window // median_runs)
        fresh = latest >= moment - timedelta(hours=eff_window)
        detail = ("" if fresh
                  else f"multi-daily ({median_runs}x/day) — older than the "
                       f"{eff_window}h window")
        return Dependency(ref.name, ref.kind, fresh, latest, minutes,
                          detail=detail, cadence=cadence, multi_daily=True)

    # A weekly or static table is checked against its own schedule.
    if ignore_non_daily or mode == "weekly":
        if cadence == "never":
            return Dependency(ref.name, ref.kind, True, latest, minutes,
                              detail="static table — ignored", cadence=cadence,
                              multi_daily=multi_daily)
        if cadence != "daily" or mode == "weekly":
            weekly_cutoff = moment - timedelta(hours=168)
            label = cadence if cadence not in ("daily", "never") else "weekly"
            if latest >= weekly_cutoff:
                return Dependency(ref.name, ref.kind, True, latest, minutes,
                                  detail=f"schedule: {label}", cadence=cadence,
                                  multi_daily=multi_daily)
            return Dependency(
                ref.name, ref.kind, False, latest, minutes,
                detail=f"schedule: {label} — older than the 168h window",
                cadence=cadence, multi_daily=multi_daily)

    # The default daily check.
    fresh = latest >= cutoff
    return Dependency(ref.name, ref.kind, fresh, latest, minutes,
                      "" if fresh else f"older than the {window}h window",
                      cadence=cadence, multi_daily=multi_daily)


def _dep_line(dep: Dependency, zone: ZoneInfo) -> str:
    if dep.ok and dep.last_refresh is None:
        return f"note   {dep.name}  ({dep.kind})  {dep.detail}"
    mark = "fresh" if dep.ok else "stale"
    line = f"{mark}  {dep.name}  ({dep.kind})"
    if dep.last_refresh is None:
        return f"{line}  {dep.detail}"
    line += (f"  last refresh {_stamp(dep.last_refresh, zone)}"
             f" ({_human_age(dep.age_minutes)} ago)")
    if dep.detail:
        line += f" — {dep.detail}"
    return line


def _stamp(value: datetime, zone: ZoneInfo) -> str:
    return value.astimezone(zone).strftime("%Y-%m-%d %H:%M %Z")


def _human_age(minutes: int | None) -> str:
    if minutes is None:
        return "?"
    if minutes < 60:
        return f"{minutes}m"
    hours, rest = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {rest:02d}m"
    days, leftover = divmod(hours, 24)
    return f"{days}d {leftover}h"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"
