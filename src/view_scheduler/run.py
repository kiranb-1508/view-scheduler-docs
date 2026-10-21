"""Entry point: view name in, report out. Read-only."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta

from .client import Workspace, failure_category, read_commits
from .config import Config
from .history import summarize
from .models import Skipped, TableHistory
from .recommend import Recommendation, recommend
from .report import render
from .walker import walk


def collect_history(view: str, ws: Workspace, config: Config,
                    today: date | None = None
                    ) -> tuple[Recommendation, list[TableHistory], list[Skipped]]:
    """Walk, read, and roll up. Shared by analyze() and advise()."""
    today = today or datetime.now(config.tz).date()
    found = walk(view, ws.get_table)
    # The advisor treats a blocker the same as a note: something it must name,
    # but cannot read. The gate keeps the two apart. See walker.py.
    skipped = list(found.blockers) + list(found.notes)

    cutoff_day = today - timedelta(days=config.days_history)
    since_epoch = int(datetime.combine(cutoff_day, time.min,
                                       tzinfo=config.tz).timestamp())

    histories: list[TableHistory] = []
    for table in found.tables:
        try:
            commits = read_commits(ws, table.name, since_epoch=since_epoch)
        except Exception as exc:
            skipped.append(Skipped(table.name, _reason(exc)))
            continue
        histories.append(summarize(table.name, commits, today, config.days_history,
                                   config.tz))

    result = recommend(histories, config.buffer_min)
    if any(item.blocks for item in skipped):
        # A time computed without one input is not a safe time. Withhold every
        # suggested time, but keep the rows and the day-by-day evidence so the
        # report still shows what we could read. `blocks=False` notes (function
        # dependencies) never reach here.
        result = replace(
            result,
            safe=None,
            earlier=None,
            usual=None,
            safe_without_outliers=None,
            safe_next_day=False,
            earlier_next_day=False,
            safe_without_outliers_next_day=False,
            usual_next_day=False,
        )
    return result, histories, skipped


collect = collect_history


def analyze(view: str, ws: Workspace, config: Config,
            today: date | None = None) -> tuple[str, list[TableHistory], list[Skipped]]:
    """The low-level call: the report text, the per-table history, the skips.

    Prefer `advise()` unless you need the raw pieces.
    """
    result, histories, skipped = collect_history(view, ws, config, today)
    return render(view, result, skipped, config.timezone), histories, skipped


def _reason(exc: Exception) -> str:
    category = failure_category(exc)
    if category == "permission":
        return "permission denied — cannot read its history"
    if category == "mv_no_history":
        return "materialized view whose pipeline event log is not readable"
    return f"history unreadable: {str(exc)[:100]}"
