"""Entry point: view name in, report out. Read-only."""

from __future__ import annotations

from datetime import date, datetime

from .client import Workspace, failure_category, read_commits
from .config import Config
from .history import summarize
from .models import Skipped, TableHistory
from .recommend import Recommendation, recommend
from .report import render
from .walker import walk


def collect(view: str, ws: Workspace, config: Config,
            today: date | None = None
            ) -> tuple[Recommendation, list[TableHistory], list[Skipped]]:
    """Walk, read, and roll up. Shared by analyze() and advise()."""
    today = today or datetime.now(config.tz).date()
    gating, skipped = walk(view, ws.get_table)

    histories: list[TableHistory] = []
    for table in gating:
        try:
            commits = read_commits(ws, table)
        except Exception as exc:
            skipped.append(Skipped(table, _reason(exc)))
            continue
        histories.append(summarize(table, commits, today, config.days_history, config.tz))

    return recommend(histories, config.buffer_min), histories, skipped


def analyze(view: str, ws: Workspace, config: Config,
            today: date | None = None) -> tuple[str, list[TableHistory], list[Skipped]]:
    """The low-level call: the report text, the per-table history, the skips.

    Prefer `advise()` unless you need the raw pieces.
    """
    result, histories, skipped = collect(view, ws, config, today)
    return render(view, result, skipped, config.timezone), histories, skipped


def _reason(exc: Exception) -> str:
    category = failure_category(exc)
    if category == "permission":
        return "permission denied — cannot read its history"
    if category == "mv_no_history":
        return "materialized view whose pipeline event log is not readable"
    return f"history unreadable: {str(exc)[:100]}"
