"""One-shot DDL for the fixture rig. Run once, before deploying the jobs.

Creates only objects inside SCHEMA. Every statement is checked against that
before it runs.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import plan  # noqa: E402
from view_scheduler import obs  # noqa: E402
from view_scheduler.client import Workspace  # noqa: E402

S = plan.SCHEMA

#: 60 days, not the 30-day default. At the default, day-1 commits age out of
#: DESCRIBE HISTORY exactly as the 30-day experiment completes. Confined to
#: tables the rig owns; no user table is altered.
RETENTION = "TBLPROPERTIES ('delta.logRetentionDuration' = '60 days')"

BASE_TABLES = plan.DRIVEN_TABLES + plan.DORMANT_TABLES


def statements() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = [
        ("drop capability probe", "DROP SCHEMA IF EXISTS workspace.rig_probe CASCADE"),
        ("create schema", f"CREATE SCHEMA IF NOT EXISTS {S}"),
    ]
    for table in BASE_TABLES:
        out.append((f"create {table}",
                    f"CREATE OR REPLACE TABLE {S}.{table} {RETENTION} AS "
                    f"SELECT id, id AS v, current_timestamp() AS seeded_at "
                    f"FROM range(10)"))

    for view, tables in plan.VIEWS.items():
        first, *rest = tables
        joins = " ".join(
            f"JOIN {S}.{t} {chr(98 + i)} ON a.id = {chr(98 + i)}.id"
            for i, t in enumerate(rest))
        out.append((f"create {view}",
                    f"CREATE OR REPLACE VIEW {S}.{view} AS "
                    f"SELECT a.id FROM {S}.{first} a {joins}"))

    # A nested diamond, so the walker is exercised on real metadata: v_deep
    # reaches t_daily_tight through two paths and must visit it once.
    out += [
        ("create v_deep_left", f"CREATE OR REPLACE VIEW {S}.v_deep_left AS "
                               f"SELECT id FROM {S}.t_daily_tight"),
        ("create v_deep_right", f"CREATE OR REPLACE VIEW {S}.v_deep_right AS "
                                f"SELECT a.id FROM {S}.t_daily_tight a "
                                f"JOIN {S}.t_merge b ON a.id = b.id"),
        ("create v_deep", f"CREATE OR REPLACE VIEW {S}.v_deep AS "
                          f"SELECT l.id FROM {S}.v_deep_left l "
                          f"JOIN {S}.v_deep_right r ON l.id = r.id"),
    ]

    # Refreshed on their own schedules, not by the slot jobs. Quartz cron:
    # seconds first, and a '?' where day-of-month and day-of-week would clash.
    out += [
        ("create mv_daily",
         f"CREATE OR REPLACE MATERIALIZED VIEW {S}.mv_daily "
         f"SCHEDULE CRON '0 30 4 * * ?' AT TIME ZONE 'Asia/Kolkata' AS "
         f"SELECT id, count(*) AS n FROM {S}.t_daily_tight GROUP BY id"),
        ("create st_stream",
         f"CREATE OR REFRESH STREAMING TABLE {S}.st_stream "
         f"SCHEDULE CRON '0 50 4 * * ?' AT TIME ZONE 'Asia/Kolkata' AS "
         f"SELECT * FROM STREAM {S}.t_daily_append"),
    ]
    return out


guard = plan.guard


def main() -> None:
    obs.setup()
    ws = Workspace(warehouse_id=os.environ["WAREHOUSE_ID"])
    print(f"setting up {S}  ({plan.RIG_START} → {plan.RIG_END})\n")
    for label, statement in statements():
        guard(statement, allow=("rig_probe",))
        try:
            ws.sql(statement, target=label)
            print(f"  ok    {label}")
        except Exception as exc:
            print(f"  FAIL  {label}: {str(exc)[:160]}")
            raise
    print(f"\ndone. {len(BASE_TABLES)} tables, {len(plan.VIEWS) + 3} views, "
          f"1 MV, 1 streaming table")


if __name__ == "__main__":
    main()
