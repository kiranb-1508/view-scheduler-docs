"""Entry point: view name in, report out. Read-only."""

from __future__ import annotations

from datetime import date, datetime

from .client import Workspace, failure_category, read_commits
from .config import Config
from .history import summarize
from .models import Skipped, TableHistory
from .recommend import recommend
from .report import render
from .walker import walk


def analyze(view: str, ws: Workspace, config: Config,
            today: date | None = None) -> tuple[str, list[TableHistory], list[Skipped]]:
    """Walk, read, roll up, render."""
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

    result = recommend(histories, config.buffer_min)
    text = render(view, result, skipped, config.timezone)
    return text, histories, skipped


def _reason(exc: Exception) -> str:
    category = failure_category(exc)
    if category == "permission":
        return "permission denied — cannot read its history"
    if category == "mv_no_history":
        return "materialized view whose pipeline event log is not readable"
    return f"history unreadable: {str(exc)[:100]}"
