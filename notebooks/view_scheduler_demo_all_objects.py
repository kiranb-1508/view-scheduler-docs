# Databricks notebook source
# MAGIC %md
# MAGIC # View Materialization Scheduler — demo on every object in `workspace.sched_rig`
# MAGIC
# MAGIC Runs the library on every object the fixture rig created (13 tables, 7 views,
# MAGIC 1 materialized view, 1 streaming table) and prints one report per object,
# MAGIC then a one-row-per-object summary.
# MAGIC
# MAGIC Advisory only. The recommended time is evidence read from history. Nothing
# MAGIC checks at runtime that a dependency refreshed.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Install the library
# MAGIC
# MAGIC The wheel lives in a Unity Catalog Volume, so access is governed by
# MAGIC `GRANT READ VOLUME`.

# COMMAND ----------

# MAGIC %pip install /Volumes/workspace/sched_rig/libs/view_scheduler-0.6.0-py3-none-any.whl

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Configure

# COMMAND ----------

from view_scheduler import Config, Workspace, analyze
from view_scheduler.recommend import recommend

WAREHOUSE_ID = "f96bd3430e175166"   # SQL warehouse used for history reads
SCHEMA = "workspace.sched_rig"

ws = Workspace(warehouse_id=WAREHOUSE_ID)
config = Config()                   # days_history=30, buffer_min=15, Asia/Kolkata
print("schema :", SCHEMA)
print("config :", config)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Every object in the schema

# COMMAND ----------

objects = sorted(row["tableName"] for row in spark.sql(f"SHOW TABLES IN {SCHEMA}").collect())
print(f"{len(objects)} objects")
objects

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Run the scheduler on each object

# COMMAND ----------

summary = []

for name in objects:
    fqn = f"{SCHEMA}.{name}"
    print("=" * 78)
    print(fqn)
    print("=" * 78)
    try:
        text, histories, skipped = analyze(fqn, ws, config)
        print(text)
        result = recommend(histories, config.buffer_min)
        summary.append({
            "object": name,
            "safe": str(result.safe) if result.safe else None,
            "safe_without_outliers": (str(result.safe_without_outliers)
                                      if result.safe_without_outliers else None),
            "usual_time": str(result.usual) if result.usual else None,
            "earlier": str(result.earlier) if result.earlier else None,
            "multi_daily": [row.name for row in result.rows if row.multi_daily],
            "days_of_evidence": result.n_days,
            "dependencies": len(result.rows),
            "skipped": len(skipped),
            "error": None,
        })
    except Exception as exc:
        print(f"  ERROR {type(exc).__name__}: {exc}")
        summary.append({
            "object": name, "safe": None, "safe_without_outliers": None,
            "usual_time": None, "earlier": None, "multi_daily": [],
            "days_of_evidence": 0, "dependencies": 0, "skipped": 0,
            "error": f"{type(exc).__name__}: {exc}"[:120],
        })

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Summary — one row per object
# MAGIC
# MAGIC `safe_without_outliers` and `usual_time` carry the outlier-aware timings.
# MAGIC `multi_daily` names the dependencies that refresh more than once on a
# MAGIC typical day, so the summary shows which runs the schedule leaves out.

# COMMAND ----------

import pandas as pd

display(pd.DataFrame(summary))

# COMMAND ----------

# MAGIC %md
# MAGIC The cell below returns the same summary as JSON, so a scheduled run can read
# MAGIC it from `notebook_output`. Skip it when running interactively.

# COMMAND ----------

import json

dbutils.notebook.exit(json.dumps(summary, default=str))
