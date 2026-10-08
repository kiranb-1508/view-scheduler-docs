"""Gate: can a view refresh now?

Give it a view, or a list of views. It walks each one down to the physical
tables it reads and checks that every one of them wrote recently.

    READY    every dependency is fresh. The refresh can proceed.
    BLOCKED  something is stale, missing, or unreadable. The report names the
             table and the view that needs it.

READ ONLY, and DECIDE ONLY. The gate never refreshes anything. It answers one
question, and the calling job acts on the answer.

A dependency is fresh when it has a qualifying write within the last
`Config.fresh_hours` hours (default 24). A streaming table is fresh by
definition. A materialized view is read from its pipeline event log. Anything
we cannot read blocks the view, because the gate fails closed.

    from view_scheduler import can_refresh

    result = can_refresh("sales.gold.customer_360")   # WAREHOUSE_ID from env
    if result.ready:
        ...run the refresh...
    else:
        print(result)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence
from zoneinfo import ZoneInfo

from .client import Workspace, failure_category, read_latest_commit, workspace_from_env
from .config import DEFAULT, Config
from .walker import walk_refs


@dataclass(frozen=True)
class Dependency:
    """One thing a view waits for, and whether it is ready."""

    name: str
    kind: str
    ok: bool
    last_refresh: datetime | None = None
    age_minutes: int | None = None
    detail: str = ""


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
    quiet: bool = False,
) -> GateResult:
    """Report whether every dependency of each view wrote recently enough.

    `views` is one view name or a list of them. `ws` defaults to a Workspace
    built from the WAREHOUSE_ID environment variable, so the common one-liner
    `can_refresh("catalog.schema.my_view")` works inside a Databricks job.
    `fresh_hours` overrides `Config.fresh_hours` for this call only. Pass `now`
    to make a test reproducible. `quiet=True` suppresses the printed report; the
    result is returned either way.
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

    workspace = ws or workspace_from_env()

    # Shared across the whole call: a table two views depend on is read once,
    # and a failure is remembered rather than retried for every view.
    meta_cache: dict[str, dict | Exception] = {}
    hist_cache: dict[str, datetime | None | Exception] = {}

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

    def latest_write(name: str):
        if name not in hist_cache:
            try:
                hist_cache[name] = read_latest_commit(
                    workspace, name, not_after=not_after)
            except Exception as exc:
                hist_cache[name] = exc
        return hist_cache[name]

    results: list[ViewResult] = []
    for view in names:
        refs, blockers, notes = walk_refs(view, fetch_meta)
        deps = [
            _measure(ref, None, cutoff, moment, window)
            if ref.kind == "STREAMING_TABLE"
            else _measure(ref, latest_write(ref.name), cutoff, moment, window)
            for ref in refs
        ]
        deps += [Dependency(item.name, "UNKNOWN", False, detail=item.reason)
                 for item in blockers]
        deps += [Dependency(item.name, "FUNCTION", True, detail=item.reason)
                 for item in notes]
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


def _measure(ref, latest, cutoff: datetime, moment: datetime,
             window: int) -> Dependency:
    """Turn one dependency's newest qualifying write into a fresh/stale verdict."""
    if ref.kind == "STREAMING_TABLE":
        return Dependency(ref.name, ref.kind, True,
                          detail="streaming table — always fresh")
    if isinstance(latest, Exception):
        return Dependency(ref.name, ref.kind, False,
                          detail=f"history unreadable ({failure_category(latest)})")
    if latest is None:
        return Dependency(ref.name, ref.kind, False,
                          detail="no qualifying refresh in history")
    minutes = max(0, int((moment - latest).total_seconds() // 60))
    fresh = latest >= cutoff
    return Dependency(ref.name, ref.kind, fresh, latest, minutes,
                      "" if fresh else f"older than the {window}h window")


def _dep_line(dep: Dependency, zone: ZoneInfo) -> str:
    if dep.ok and dep.last_refresh is None:
        return f"note   {dep.name}  ({dep.kind})  {dep.detail}"
    mark = "fresh" if dep.ok else "stale"
    line = f"{mark}  {dep.name}  ({dep.kind})"
    if dep.last_refresh is None:
        return f"{line}  {dep.detail}"
    line += (f"  last refresh {_stamp(dep.last_refresh, zone)}"
             f" ({_human_age(dep.age_minutes)} ago)")
    if not dep.ok and dep.detail:
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
