"""Create a scratch schema of tables exercising every operation class.

NOT part of the scheduler package. The scheduler itself is strictly read-only;
this is a test rig that writes, and it is kept outside src/ deliberately so the
two cannot be confused.

It only ever touches tables inside its own scratch schema, and every statement
is checked against that before it runs. It creates nothing outside it and drops
nothing it did not create.

What this rig can and cannot prove:

  * CAN: that qualifying() classifies commits produced by real writers
    correctly — the step the design calls most likely to be silently wrong.
  * CANNOT: anything about the day-spread arithmetic. Delta assigns a commit's
    timestamp when the commit happens and there is no supported way to set it,
    so every commit this makes is dated today. Thirty days of history takes
    thirty days.
"""

from __future__ import annotations

import os
import sys

from scheduler import obs
from scheduler.client import Workspace

SCHEMA = os.environ.get("SEED_SCHEMA", "workspace.sched_fixture")


def guard(statement: str) -> None:
    """Refuse anything that does not stay inside the scratch schema."""
    if SCHEMA not in statement:
        raise SystemExit(f"refusing statement outside {SCHEMA}:\n  {statement}")
    if "sched_fixture" not in SCHEMA:
        raise SystemExit(f"scratch schema name must contain 'sched_fixture', got {SCHEMA}")


#: (label, statement). Each produces a commit we then classify.
STEPS: list[tuple[str, str]] = [
    ("create schema", f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"),

    # full replace, rows > 0
    ("ctas", f"CREATE OR REPLACE TABLE {SCHEMA}.seed_overwrite AS "
             "SELECT id, id * 2 AS v FROM range(100)"),
    ("insert overwrite", f"INSERT OVERWRITE {SCHEMA}.seed_overwrite "
                         "SELECT id, id * 3 FROM range(50)"),
    # full replace producing zero rows — must still qualify
    ("zero-row overwrite", f"INSERT OVERWRITE {SCHEMA}.seed_overwrite "
                           "SELECT id, id FROM range(0)"),

    # incremental
    ("create append target", f"CREATE OR REPLACE TABLE {SCHEMA}.seed_append AS "
                             "SELECT id, id AS v FROM range(10)"),
    ("append", f"INSERT INTO {SCHEMA}.seed_append SELECT id, id FROM range(10, 20)"),
    ("append zero rows", f"INSERT INTO {SCHEMA}.seed_append "
                         "SELECT id, id FROM range(0)"),
    ("update", f"UPDATE {SCHEMA}.seed_append SET v = v + 1 WHERE id < 5"),
    ("merge", f"MERGE INTO {SCHEMA}.seed_append t "
              "USING (SELECT id, id * 10 AS v FROM range(15, 25)) s "
              "ON t.id = s.id "
              "WHEN MATCHED THEN UPDATE SET t.v = s.v "
              "WHEN NOT MATCHED THEN INSERT (id, v) VALUES (s.id, s.v)"),

    # maintenance — must never qualify
    ("delete", f"DELETE FROM {SCHEMA}.seed_append WHERE id = 1"),
    ("optimize", f"OPTIMIZE {SCHEMA}.seed_append"),
    ("set tblproperties", f"ALTER TABLE {SCHEMA}.seed_append "
                          "SET TBLPROPERTIES ('seed.rig' = 'true')"),

    # a view over the seeded tables, so the walker has something real to resolve
    ("view", f"CREATE OR REPLACE VIEW {SCHEMA}.seed_view AS "
             f"SELECT a.id, a.v AS av, b.v AS bv "
             f"FROM {SCHEMA}.seed_overwrite a "
             f"JOIN {SCHEMA}.seed_append b ON a.id = b.id"),
]


def main() -> None:
    obs.setup()
    warehouse = os.environ["WAREHOUSE_ID"]
    ws = Workspace(warehouse_id=warehouse)
    print(f"seeding {SCHEMA}\n")
    for label, statement in STEPS:
        guard(statement)
        ws.sql(statement, target=label)
        print(f"  ok  {label}")
    print(f"\ndone. tables under {SCHEMA}")


if __name__ == "__main__":
    main()
