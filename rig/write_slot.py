"""The job task. Asks plan.py what this slot writes today, then writes it.

Runs as a Databricks notebook/python task on serverless. Takes one parameter,
`slot`, matching an entry in plan.SLOTS.

Two things it will refuse to do: touch anything outside plan.SCHEMA, and write
at all once the window has closed — at which point it pauses every rig
job so nothing runs on indefinitely.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

# plan.py sits beside this file in the synced workspace directory, but that
# directory is not on the path. Serverless runs the task through exec(), where
# __file__ is undefined, so fall back to argv[0] — which the runner sets to the
# python_file path.
def _here() -> str:
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:
        return os.path.dirname(os.path.abspath(sys.argv[0]))


sys.path.insert(0, _here())

import plan  # noqa: E402

TZ = ZoneInfo("Asia/Kolkata")
S = plan.SCHEMA
JOB_TAG = "sched_rig"


def statement_for(write: plan.Write, day) -> str:
    """The SQL for one planned write.

    Row counts come from range(), so a zero-row write is a real statement that
    commits with zero rows rather than a statement that is skipped — the
    distinction the analyzer is being tested on.
    """
    table = f"{S}.{write.table}"
    stamp = day.isoformat()

    if write.kind == plan.OVERWRITE:
        return (f"INSERT OVERWRITE {table} "
                f"SELECT id, id, current_timestamp() FROM range({write.rows})")
    if write.kind == plan.APPEND:
        return (f"INSERT INTO {table} "
                f"SELECT id, id, current_timestamp() FROM range({write.rows})")
    if write.kind == plan.MERGE:
        return (f"MERGE INTO {table} t USING ("
                f"SELECT id, id * 10 AS v FROM range({write.rows})) s "
                f"ON t.id = s.id "
                f"WHEN MATCHED THEN UPDATE SET t.v = s.v "
                f"WHEN NOT MATCHED THEN INSERT (id, v, seeded_at) "
                f"VALUES (s.id, s.v, current_timestamp())")
    if write.kind == plan.TOUCH:
        return (f"ALTER TABLE {table} SET TBLPROPERTIES "
                f"('rig.touched' = '{stamp}')")
    raise ValueError(f"unknown write kind: {write.kind}")


guard = plan.guard


def pause_all_rig_jobs() -> None:
    """Stop the rig once the window has closed. Idempotent."""
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.jobs import PauseStatus

    w = WorkspaceClient()
    paused = 0
    for job in w.jobs.list():
        name = (job.settings.name if job.settings else "") or ""
        if JOB_TAG not in name:
            continue
        settings = w.jobs.get(job.job_id).settings
        schedule = settings.schedule if settings else None
        if schedule and schedule.pause_status != PauseStatus.PAUSED:
            schedule.pause_status = PauseStatus.PAUSED
            w.jobs.update(job_id=job.job_id, new_settings=type(settings)(schedule=schedule))
            paused += 1
    print(f"window closed — paused {paused} rig job(s)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot", required=True)
    args = parser.parse_args(argv)

    today = datetime.now(TZ).date()
    print(f"slot={args.slot} day={today} window={plan.RIG_START}..{plan.RIG_END}")

    if today < plan.RIG_START:
        # Deployed ahead of the window. Do nothing, and above all do not pause:
        # pausing here would stop the rig before it ever ran a single day.
        print("before the window opens — nothing to do")
        return 0

    if today > plan.RIG_END:
        pause_all_rig_jobs()
        return 0

    writes = plan.writes_for(args.slot, today)
    if not writes:
        print("nothing planned for this slot today")
        return 0

    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()

    for write in writes:
        statement = statement_for(write, today)
        guard(statement)
        spark.sql(statement)
        print(f"  ok  {write.table:<20} {write.kind:<10} rows={write.rows}")

    print(f"wrote {len(writes)} statement(s)")
    return 0


if __name__ == "__main__":
    # Called, not sys.exit()ed: serverless runs this file inside an IPython
    # shell where a SystemExit propagates and fails the workload even at code 0.
    # The first smoke run wrote all nine statements and was still marked FAILED
    # for exactly that reason. A real error still fails the task by raising.
    main()
