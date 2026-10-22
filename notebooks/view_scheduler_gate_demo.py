# Databricks notebook source
# MAGIC %md
# MAGIC # Gate demo: `can_refresh`
# MAGIC
# MAGIC One question: **can this view refresh now?**
# MAGIC
# MAGIC Give it a view, or a list of views. It walks every dependency and checks
# MAGIC that each one wrote inside a rolling window (24 hours by default).
# MAGIC
# MAGIC - `READY` — every dependency is fresh. The refresh can proceed.
# MAGIC - `BLOCKED` — the report names the stale table and the view that needs it.
# MAGIC
# MAGIC Read-only, decide-only. It never refreshes anything.

# COMMAND ----------

# MAGIC %pip install /Volumes/workspace/sched_rig/libs/view_scheduler-0.7.0-py3-none-any.whl

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Setup

# COMMAND ----------

import json
from datetime import datetime

from view_scheduler import Config, Workspace, can_refresh

WAREHOUSE_ID = "f96bd3430e175166"
SCHEMA = "workspace.sched_rig"

ws = Workspace(warehouse_id=WAREHOUSE_ID)
config = Config()

print("timezone      :", config.timezone)
print("fresh window  :", config.fresh_hours, "hours")
print("reference now :", datetime.now(config.tz).strftime("%Y-%m-%d %H:%M %Z"))


def compact(result):
    """A small, JSON-ready summary for the exit value."""
    return {
        "ready": result.ready,
        "fresh_hours": result.fresh_hours,
        "blockers": list(result.blockers()),
        "views": [
            {
                "view": view.view.split(".")[-1],
                "ready": view.ready,
                "deps": [
                    {
                        "name": dep.name.split(".")[-1],
                        "kind": dep.kind,
                        "ok": dep.ok,
                        "age_min": dep.age_minutes,
                        "detail": dep.detail,
                    }
                    for dep in view.dependencies
                ],
            }
            for view in result.views
        ],
    }


summary = {}

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. One view
# MAGIC
# MAGIC This is the whole API. `WAREHOUSE_ID` can come from the environment, so
# MAGIC `can_refresh("catalog.schema.view")` is enough inside a job.

# COMMAND ----------

healthy = can_refresh(f"{SCHEMA}.v_healthy", ws, config)
summary["v_healthy"] = compact(healthy)
assert healthy.ready is True, "v_healthy should be ready"

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. A list of views
# MAGIC
# MAGIC One call. One verdict per view, plus one overall flag.

# COMMAND ----------

views = ["v_healthy", "v_midnight", "v_realistic", "v_broken", "v_deep"]
batch = can_refresh([f"{SCHEMA}.{v}" for v in views], ws, config)

print()
print("overall ready :", batch.ready)
print("blockers      :", batch.blockers())

summary["batch_24h"] = compact(batch)

assert batch.ready is False, "v_broken depends on a dormant table and must block"
assert any(v.view.endswith("v_broken") and not v.ready for v in batch.views)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. The rolling window
# MAGIC
# MAGIC The window is one setting for the whole call. Widen it and a table that
# MAGIC refreshes rarely stops blocking. A table that never wrote still blocks,
# MAGIC whatever the window — that is the fail-closed rule.

# COMMAND ----------

for hours in (24, 720):
    wide = can_refresh([f"{SCHEMA}.v_realistic", f"{SCHEMA}.v_broken"],
                       ws, config, fresh_hours=hours)
    print()
    print(f"window {hours}h -> ready =", wide.ready)
    assert wide.views[1].ready is False, "a table that never wrote must always block"
    summary[f"window_{hours}h"] = compact(wide)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Special object types
# MAGIC
# MAGIC A streaming table is fresh by definition. A materialized view has no
# MAGIC `DESCRIBE HISTORY`, so the gate reads its pipeline event log.

# COMMAND ----------

for name in ["st_stream", "mv_daily"]:
    summary[name] = compact(can_refresh(f"{SCHEMA}.{name}", ws, config))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. The structured result
# MAGIC
# MAGIC `to_dict()` is JSON-ready, so a job can log it or branch on it.

# COMMAND ----------

print(json.dumps(batch.to_dict(), indent=2)[:1600])

# COMMAND ----------

# MAGIC %md
# MAGIC ## Summary

# COMMAND ----------

print()
print("===== GATE DEMO SUMMARY =====")
for key, payload in summary.items():
    ready = sum(1 for v in payload["views"] if v["ready"])
    print(f"{key:<14} ready={str(payload['ready']):<5} "
          f"({ready}/{len(payload['views'])} views)  blockers={payload['blockers']}")

dbutils.notebook.exit(json.dumps(summary, default=str))
