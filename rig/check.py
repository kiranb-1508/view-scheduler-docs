"""Compare what the rig actually produced against what the plan predicted.

Run at each checkpoint. Reads real history through the same code path the
analyzer uses, then diffs it against rig/oracle.py.

    WAREHOUSE_ID=... python rig/check.py            # every view
    WAREHOUSE_ID=... python rig/check.py v_realistic

Exit code is non-zero if anything disagrees, so it can gate a build.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import oracle  # noqa: E402
import plan  # noqa: E402
from view_scheduler import obs  # noqa: E402
from view_scheduler.client import Workspace, read_commits  # noqa: E402
from view_scheduler.config import Config  # noqa: E402
from view_scheduler.recommend import recommend  # noqa: E402
from view_scheduler.run import analyze  # noqa: E402

CONFIG = Config()

#: Refresh on their own Databricks schedules, not the slot matrix, so the plan
#: cannot predict their exact times. They are validated for readability only.
SCHEDULE_DRIVEN = {
    "mv_daily": "materialized view (event-log fallback)",
    "st_stream": "streaming table",
}


def qualified(table: str) -> str:
    return f"{plan.SCHEMA}.{table}"


def check_view(ws: Workspace, view: str, tables: list[str], today) -> list[str]:
    text, histories, skipped = analyze(qualified(view), ws, CONFIG, today=today)
    print(text)
    print()

    actual = recommend(histories, CONFIG.buffer_min)
    expected = oracle.expected_recommendation(
        tables, today, CONFIG.buffer_min, CONFIG.days_history)
    names = {t: qualified(t) for t in tables}
    problems = oracle.mismatches(expected, actual, names)
    for item in skipped:
        problems.append(f"unexpectedly skipped: {item.name} — {item.reason}")
    return problems


def check_schedule_driven(ws: Workspace, today) -> list[str]:
    """The only real-data coverage of the materialized-view event-log fallback.

    mv_daily rejects DESCRIBE HISTORY and must be read through the event log;
    st_stream reads through DESCRIBE HISTORY. Neither is predictable from the
    plan, so assert only that its history is readable and non-empty in window.
    """
    problems: list[str] = []
    for name, what in SCHEDULE_DRIVEN.items():
        try:
            commits = read_commits(ws, qualified(name))
        except Exception as exc:
            problems.append(f"{name} ({what}): history unreadable — {exc}")
            continue
        in_window = [c for c in commits
                     if c.timestamp.astimezone(CONFIG.tz).date() >= plan.RIG_START]
        if in_window:
            print(f"  {name}: {len(in_window)} refresh(es) readable via {what}")
        else:
            problems.append(f"{name} ({what}): no refresh in the window")
    return problems


def main() -> int:
    obs.setup(level=40)
    ws = Workspace(warehouse_id=os.environ["WAREHOUSE_ID"])
    today = datetime.now(CONFIG.tz).date()
    wanted = sys.argv[1:] or sorted(plan.VIEWS)

    elapsed = (today - plan.RIG_START).days + 1
    print(f"checkpoint: day {elapsed} of {plan.RIG_DAYS} "
          f"({plan.RIG_START} → {plan.RIG_END})\n")
    if elapsed < plan.RIG_DAYS:
        print(f"NOTE: the rig is still running. Days beyond today have not "
              f"happened yet,\n      so the oracle is clipped to what should "
              f"exist so far.\n")

    failures = 0
    for view in wanted:
        print("=" * 72)
        problems = check_view(ws, view, plan.VIEWS[view], today)
        if problems:
            failures += 1
            print(f"  {len(problems)} MISMATCH(ES):")
            for problem in problems:
                print(f"    - {problem}")
        else:
            print("  matches the plan")
        print()

    # Only run the schedule-driven smoke on a full checkpoint, not a single view.
    if len(sys.argv) == 1:
        print("=" * 72)
        print("schedule-driven objects (mv_daily, st_stream):")
        sd_problems = check_schedule_driven(ws, today)
        for problem in sd_problems:
            print(f"    - {problem}")
        failures += bool(sd_problems)
        print()

    print("=" * 72)
    print(f"{len(wanted) - failures}/{len(wanted)} checks match the plan")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
