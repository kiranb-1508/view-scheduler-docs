# Databricks notebook source
# MAGIC %md
# MAGIC # Advice demo: `advise`
# MAGIC
# MAGIC One question: **when each day should this view run?**
# MAGIC
# MAGIC It reads the history of every upstream job and suggests a time.
# MAGIC
# MAGIC - `SCHEDULE AT` — the time that would have worked on every day.
# MAGIC - `EARLIER` — the typical day. It is the middle normal day plus the
# MAGIC   buffer. Outlier reruns do not move it.
# MAGIC
# MAGIC This is a suggestion from history. For a live check, use `can_refresh`.

# COMMAND ----------

# MAGIC %pip install /Volumes/workspace/sched_rig/libs/view_scheduler-0.6.0-py3-none-any.whl

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Setup

# COMMAND ----------

import json

from view_scheduler import Config, Workspace, advise

SCHEMA = "workspace.sched_rig"
ws = Workspace(warehouse_id="f96bd3430e175166")
config = Config()

print("timezone:", config.timezone, "| history:", config.days_history, "days")
summary = {}

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. A healthy view
# MAGIC
# MAGIC Both upstream jobs run daily. The advice should be a real time.

# COMMAND ----------

advice = advise(f"{SCHEMA}.v_healthy", ws, config)
summary["v_healthy"] = advice.to_dict()
assert advice.safe_time is not None, "a daily view should get a time"

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. A view with a weekly job
# MAGIC
# MAGIC One upstream job runs on Mondays only, so the common days are few. Watch
# MAGIC `DAYS` and the warnings — thin evidence is shown, not hidden.

# COMMAND ----------

weekly = advise(f"{SCHEMA}.v_realistic", ws, config)
summary["v_realistic"] = weekly.to_dict()

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. The structured result
# MAGIC
# MAGIC `advise` returns an `Advice`. The report is text; the same numbers are fields.

# COMMAND ----------

print()
print("safe_time    :", advice.safe_time)
print("earlier_time :", advice.earlier_time)
print("next_day     :", advice.next_day)
print("days_used    :", advice.days_used, "of", advice.period_days)
print("dependencies :")
for row in advice.dependencies:
    print("   ", row.name.split(".")[-1].ljust(20),
          "runs=", row.runs.ljust(8),
          "usual=", row.usual, "latest=", row.latest,
          "days=", row.days, "missed=", row.missed)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. A view that cannot run yet
# MAGIC
# MAGIC `v_broken` has a job that never writes. With no day where every job ran,
# MAGIC there is no time to suggest. The report says so.

# COMMAND ----------

broken = advise(f"{SCHEMA}.v_broken", ws, config)
summary["v_broken"] = broken.to_dict()
assert broken.safe_time is None, "a dormant upstream means no time"

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. JSON
# MAGIC
# MAGIC `to_dict()` is ready for a job log.

# COMMAND ----------

print(json.dumps(advice.to_dict(), indent=2)[:1200])

# COMMAND ----------

# MAGIC %md
# MAGIC ## Summary

# COMMAND ----------

print()
print("===== ADVICE DEMO SUMMARY =====")
for key, payload in summary.items():
    print(key.ljust(12),
          "safe_time=", payload["safe_time"],
          " earlier_time=", payload["earlier_time"],
          " days_used=", payload["days_used"],
          " unreadable=", len(payload["unreadable"]))

dbutils.notebook.exit(json.dumps(summary, default=str))
