"""Gate: is a view safe to refresh now?

Given a view, or a list of views, walk each one down to its physical tables and
ask one question per table: did it write within the rolling window?

    READY   — every dependency is fresh. Refresh can proceed.
    BLOCKED — one or more dependencies are stale, missing, or unreadable. The
              gate names the table and the view that needs it.

READ ONLY, and decide only. The gate never triggers a refresh. It answers a
question; the calling job acts on the answer.

A dependency is fresh when it has a qualifying write within `Config.fresh_hours`
(default 24) of the reference time. A streaming table is fresh by definition. A
materialized view is read from its pipeline event log. Anything that cannot be
read blocks the view (fail closed).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence
from zoneinfo import ZoneInfo

from .client import Workspace, failure_category, read_commits
from .config import DEFAULT, Config
from .history import qualifying
from .models import Skipped
from .walker import GatingRef, walk_refs


@dataclass(frozen=True)
class DepStatus:
    """One gating dependency and whether it is fresh enough."""

    name: str
    kind: str
    fresh: bool
    last_refresh: datetime | None = None
    age_minutes: int | None = None
    reason: str = ""


@dataclass(frozen=True)
class ViewGate:
    """The gate verdict for one view."""

    view: str
    ready: bool
    dependencies: tuple[DepStatus, ...] = ()
    notes: tuple[Skipped, ...] = ()
    blockers: tuple[Skipped, ...] = ()


@dataclass(frozen=True)
class GateResult:
    """The gate verdict for a whole call."""

    ready: bool
    views: tuple[ViewGate, ...]
    fresh_hours: int
    timezone: str = "Asia/Kolkata"

    def blockers(self) -> list[tuple[str, str]]:
        """(view, blocker name) for every reason a view is not ready."""
        found: list[tuple[str, str]] = []
        for view in self.views:
            for dep in view.dependencies:
                if not dep.fresh:
                    found.append((view.view, dep.name))
            for skip in view.blockers:
                found.append((view.view, skip.name))
        return found

    def to_dict(self) -> dict:
        """JSON-ready. Safe to log from a job."""
        return {
            "ready": self.ready,
            "fresh_hours": self.fresh_hours,
            "timezone": self.timezone,
            "views": [
                {
                    "view": view.view,
                    "ready": view.ready,
                    "dependencies": [
                        {
                            "name": dep.name,
                            "kind": dep.kind,
                            "fresh": dep.fresh,
                            "last_refresh": (
                                dep.last_refresh.isoformat()
                                if dep.last_refresh else None
                            ),
                            "age_minutes": dep.age_minutes,
                            "reason": dep.reason,
                        }
                        for dep in view.dependencies
                    ],
                    "blockers": [
                        {"name": s.name, "reason": s.reason} for s in view.blockers
                    ],
                    "notes": [
                        {"name": s.name, "reason": s.reason} for s in view.notes
                    ],
                }
                for view in self.views
            ],
        }

    def render(self) -> str:
        """The human-readable report. One block per view, then the verdict."""
        lines: list[str] = []
        zone = ZoneInfo(self.timezone)
        for view in self.views:
            head = "READY  " if view.ready else "BLOCKED"
            lines.append(f"{head} {view.view}   (window {self.fresh_hours}h)")
            for dep in sorted(view.dependencies, key=lambda d: (d.fresh, d.name)):
                mark = "ok   " if dep.fresh else "stale"
                when = _stamp(dep.last_refresh, zone)
                age = _age(dep.age_minutes)
                why = f"  {dep.reason}" if dep.reason and not dep.fresh else ""
                lines.append(f"  {mark} {dep.name}  {dep.kind}  "
                             f"last refresh {when}  ({age} ago){why}")
            for skip in view.blockers:
                lines.append(f"  block {skip.name}  {skip.reason}")
            for note in view.notes:
                lines.append(f"  note  {note.name}  {note.reason}")
        ready = sum(1 for view in self.views if view.ready)
        verdict = "READY" if self.ready else "BLOCKED"
        lines.append("")
        lines.append(f"GATE RESULT  {verdict}   {ready} of {len(self.views)} views ready")
        return "\n".join(lines)

    def __str__(self) -> str:
        return self.render()


def can_refresh(
    views: str | Sequence[str],
    ws: Workspace,
    config: Config = DEFAULT,
    *,
    now: datetime | None = None,
    fresh_hours: int | None = None,
    quiet: bool = False,
) -> GateResult:
    """Report whether every dependency of each view wrote in the rolling window.

    Args:
        views: one view name, or a sequence of view names.
        ws: the read-only Workspace (metadata and history).
        config: supplies the timezone and the default window.
        now: reference time. Defaults to now() in the config timezone. Pass one
            in tests to make the result deterministic.
        fresh_hours: override the window for this call only.
        quiet: do not print the report. The result is returned either way.

    Returns:
        A GateResult. `ready` is True only when every listed view is ready.
    """
    names = [views] if isinstance(views, str) else list(views)
    if not names:
        raise ValueError("can_refresh needs at least one view name")

    window = config.fresh_hours if fresh_hours is None else fresh_hours
    moment = now or datetime.now(config.tz)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=config.tz)
    cutoff = moment - timedelta(hours=window)

    # Shared across the whole call, so a table two views depend on is read once.
    meta_cache: dict[str, dict] = {}
    hist_cache: dict[str, datetime | None | Exception] = {}

    def fetch_meta(name: str) -> dict:
        if name not in meta_cache:
            meta_cache[name] = ws.get_table(name)
        return meta_cache[name]

    def latest_qualifying(name: str) -> datetime | None | Exception:
        if name not in hist_cache:
            try:
                commits = read_commits(ws, name)
            except Exception as exc:          # fail closed: noted, not raised
                hist_cache[name] = exc
                return exc
            newest: datetime | None = None
            for commit in commits:
                if qualifying(commit) and (newest is None or commit.timestamp > newest):
                    newest = commit.timestamp
            hist_cache[name] = newest
        return hist_cache[name]

    gates: list[ViewGate] = []
    for view in names:
        refs, skipped = walk_refs(view, fetch_meta)
        deps: list[DepStatus] = []
        for ref in refs:
            deps.append(_status(ref, None, window) if ref.kind == "STREAMING_TABLE"
                        else _status(ref, latest_qualifying(ref.name), window,
                                     cutoff=cutoff, moment=moment))
        notes = tuple(s for s in skipped if s.benign)
        blockers = tuple(s for s in skipped if not s.benign)
        ready = all(dep.fresh for dep in deps) and not blockers
        gates.append(ViewGate(view, ready, tuple(deps), notes, blockers))

    result = GateResult(
        ready=all(gate.ready for gate in gates),
        views=tuple(gates),
        fresh_hours=window,
        timezone=config.timezone,
    )
    if not quiet:
        print(result)
    return result


def _status(ref: GatingRef, latest: datetime | None | Exception, window: int,
            *, cutoff: datetime | None = None,
            moment: datetime | None = None) -> DepStatus:
    """Turn one dependency's newest qualifying write into a fresh/stale verdict."""
    if ref.kind == "STREAMING_TABLE":
        return DepStatus(ref.name, ref.kind, True, None, None,
                         "streaming table — always fresh")
    if isinstance(latest, Exception):
        return DepStatus(ref.name, ref.kind, False, None, None,
                         f"history unreadable ({failure_category(latest)})")
    if latest is None:
        return DepStatus(ref.name, ref.kind, False, None, None,
                         "no qualifying refresh in history")
    assert cutoff is not None and moment is not None
    minutes = max(0, int((moment - latest).total_seconds() // 60))
    fresh = latest >= cutoff
    return DepStatus(ref.name, ref.kind, fresh, latest, minutes,
                     "" if fresh else f"outside the {window}h window")


def _stamp(value: datetime | None, zone: ZoneInfo) -> str:
    if value is None:
        return "never"
    return value.astimezone(zone).strftime("%Y-%m-%d %H:%M %Z")


def _age(minutes: int | None) -> str:
    if minutes is None:
        return "-"
    hours, rest = divmod(minutes, 60)
    return f"{hours}h {rest:02d}m" if hours else f"{rest}m"
