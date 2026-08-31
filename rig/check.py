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
from scheduler import obs  # noqa: E402
from scheduler.client import Workspace  # noqa: E402
from scheduler.config import Config  # noqa: E402
from scheduler.recommend import recommend  # noqa: E402
from scheduler.run import analyze  # noqa: E402

CONFIG = Config()


def qualified(table: str) -> str:
    return f"{plan.SCHEMA}.{table}"


def check_view(ws: Workspace, view: str, tables: list[str], today) -> list[str]:
    text, histories, skipped = analyze(qualified(view), ws, CONFIG, today=today)
    print(text)
    print()

    actual = recommend(histories, CONFIG.buffer_min)
    names = {t: qualified(t) for t in tables}
    problems = oracle.mismatches(oracle.expected(tables, today, CONFIG.buffer_min,
                                                 CONFIG.days_history),
                                 actual, names)
    for item in skipped:
        problems.append(f"unexpectedly skipped: {item.name} — {item.reason}")
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

    print("=" * 72)
    print(f"{len(wanted) - failures}/{len(wanted)} views match the plan")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
