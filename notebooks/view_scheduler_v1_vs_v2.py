# Databricks notebook source
# MAGIC %md
# MAGIC # V1 vs V2 — comparison and performance on Databricks
# MAGIC
# MAGIC **V1** = `view_scheduler` (the packaged library). I/O through the SDK plus the
# MAGIC SQL Statement Execution API. Works from anywhere with a warehouse.
# MAGIC
# MAGIC **V2** = `lazy_scheduler` (the single-module simplification). I/O through the
# MAGIC SDK plus `spark.sql`. Needs a Spark session, so it runs inside Databricks.
# MAGIC
# MAGIC Same objects, same window, same buffer. Measures wall-clock end-to-end,
# MAGIC then pure compute with I/O removed, and compares the answers.

# COMMAND ----------

# MAGIC %pip install /Volumes/workspace/sched_rig/libs/view_scheduler-0.1.0-py3-none-any.whl

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Setup

# COMMAND ----------

import json
import statistics
import sys
import time
from datetime import date

sys.path.insert(0, "/Volumes/workspace/sched_rig/libs/v2")

import lazy_scheduler as v2
from view_scheduler.client import Workspace
from view_scheduler.config import Config as V1Config
from view_scheduler.recommend import recommend as v1_recommend
from view_scheduler.run import analyze as v1_analyze

WAREHOUSE_ID = "f96bd3430e175166"
SCHEMA = "workspace.sched_rig"
TODAY = date(2026, 10, 7)          # pinned, so both see the same window
REPEATS = 3                        # cold runs per object per library
WARM_RUNS = 5                      # pure-compute runs per object per library

spark.conf.set("spark.sql.session.timeZone", "Asia/Kolkata")

V1_CFG = V1Config()                # days_history=30, buffer_min=15, Asia/Kolkata
V2_CFG = v2.Config()               # the same three parameters

ws = Workspace(warehouse_id=WAREHOUSE_ID)
client = ws.w                      # the SDK client, for V2's metadata path

OBJECTS = [
    "v_healthy", "v_realistic", "v_midnight", "v_broken",
    "v_deep", "mv_daily", "st_stream",
]


def quote(fqn):
    return ".".join(f"`{part}`" for part in fqn.split("."))


def fmt_time(t):
    return "--" if t is None else f"{t.hour:02d}:{t.minute:02d}"


def fmt_min(m):
    return "--" if m is None else f"{m // 60:02d}:{m % 60:02d}"


def med(values):
    return round(statistics.median(values), 1)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. The two I/O paths
# MAGIC
# MAGIC Each class caches every call, so the second run on the same source is pure
# MAGIC compute. The counters show how many calls each library makes.

# COMMAND ----------

class V1Sources:
    """Duck-types view_scheduler.Workspace: get_table() and sql()."""

    def __init__(self):
        self.meta, self.rows = {}, {}
        self.get_calls = self.sql_calls = 0

    def get_table(self, name):
        self.get_calls += 1
        if name not in self.meta:
            self.meta[name] = ws.get_table(name)
        return self.meta[name]

    def sql(self, statement, target):
        self.sql_calls += 1
        if target not in self.rows:
            self.rows[target] = ws.sql(statement, target)
        return self.rows[target]


class V2Sources:
    """V2's two seams: fetch_meta() and fetch_history()."""

    def __init__(self):
        self.meta, self.rows = {}, {}
        self.meta_calls = self.hist_calls = 0

    def fetch_meta(self, name):
        self.meta_calls += 1
        if name not in self.meta:
            self.meta[name] = client.tables.get(full_name=name).as_dict()
        return self.meta[name]

    def fetch_history(self, name):
        self.hist_calls += 1
        if name not in self.rows:
            rows = spark.sql(f"DESCRIBE HISTORY {quote(name)}").collect()
            self.rows[name] = [r.asDict(recursive=True) for r in rows]
        return self.rows[name]

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Warm-up
# MAGIC
# MAGIC Excluded from the numbers. Starts the SQL warehouse and the Spark session.

# COMMAND ----------

t0 = time.perf_counter()
ws.get_table(f"{SCHEMA}.v_healthy")
print(f"warm-up (V1 metadata path) {(time.perf_counter() - t0) * 1000:,.0f} ms")

t0 = time.perf_counter()
client.tables.get(full_name=f"{SCHEMA}.v_healthy")
print(f"warm-up (V2 metadata path) {(time.perf_counter() - t0) * 1000:,.0f} ms")

t0 = time.perf_counter()
spark.sql(f"DESCRIBE HISTORY {quote(f'{SCHEMA}.t_daily_tight')}").collect()
print(f"warm-up (V2 history path)  {(time.perf_counter() - t0) * 1000:,.0f} ms")

t0 = time.perf_counter()
ws.sql(f"SELECT count(*) c FROM (DESCRIBE HISTORY {SCHEMA}.t_daily_tight)", target="warm")
print(f"warm-up (V1 history path)  {(time.perf_counter() - t0) * 1000:,.0f} ms")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Measure

# COMMAND ----------

def measure(view):
    fqn = f"{SCHEMA}.{view}"
    row = {"object": view}

    # ---- V1: end-to-end, fresh I/O each run ----
    cold, warm, stats = [], [], None
    for i in range(REPEATS):
        src = V1Sources()
        t0 = time.perf_counter()
        try:
            text, histories, skipped = v1_analyze(fqn, src, V1_CFG, TODAY)
            cold.append((time.perf_counter() - t0) * 1000)
            if i == 0:
                rec = v1_recommend(histories, V1_CFG.buffer_min)
                stats = {
                    "safe": fmt_time(rec.safe), "tighter": fmt_time(rec.tighter),
                    "days": rec.n_days, "deps": len(rec.rows),
                    "skipped": len(skipped),
                    "uc": src.get_calls, "sql": src.sql_calls,
                }
        except Exception as exc:
            row["v1_error"] = f"{type(exc).__name__}: {exc}"[:120]
            break
    if not cold:
        return row

    src = V1Sources()
    v1_analyze(fqn, src, V1_CFG, TODAY)                  # prime
    for _ in range(WARM_RUNS):
        t0 = time.perf_counter()
        v1_analyze(fqn, src, V1_CFG, TODAY)
        warm.append((time.perf_counter() - t0) * 1000)

    # ---- V2: end-to-end, fresh I/O each run ----
    cold2, warm2, stats2 = [], [], None
    for i in range(REPEATS):
        src2 = V2Sources()
        t0 = time.perf_counter()
        try:
            report = v2.analyze(fqn, src2.fetch_meta, src2.fetch_history, TODAY, V2_CFG)
            cold2.append((time.perf_counter() - t0) * 1000)
            if i == 0:
                r = report.recommendation
                stats2 = {
                    "safe": fmt_min(r.safe_min), "tighter": fmt_min(r.sooner_min),
                    "days": len(r.all_clear), "deps": len(report.dependencies),
                    "skipped": len(report.skipped),
                    "meta": src2.meta_calls, "hist": src2.hist_calls,
                }
        except Exception as exc:
            row["v2_error"] = f"{type(exc).__name__}: {exc}"[:120]
            break
    if cold2:
        src2 = V2Sources()
        v2.analyze(fqn, src2.fetch_meta, src2.fetch_history, TODAY, V2_CFG)   # prime
        for _ in range(WARM_RUNS):
            t0 = time.perf_counter()
            v2.analyze(fqn, src2.fetch_meta, src2.fetch_history, TODAY, V2_CFG)
            warm2.append((time.perf_counter() - t0) * 1000)

    row.update({
        "v1_cold_ms": med(cold), "v2_cold_ms": med(cold2) if cold2 else None,
        "v1_warm_ms": med(warm), "v2_warm_ms": med(warm2) if warm2 else None,
        "v1_safe": stats["safe"], "v2_safe": stats2["safe"] if stats2 else None,
        "v1_tighter": stats["tighter"], "v2_sooner": stats2["tighter"] if stats2 else None,
        "v1_days": stats["days"], "v2_days": stats2["days"] if stats2 else None,
        "v1_deps": stats["deps"], "v2_deps": stats2["deps"] if stats2 else None,
        "v1_skipped": stats["skipped"], "v2_skipped": stats2["skipped"] if stats2 else None,
        "v1_uc_calls": stats["uc"], "v1_sql_calls": stats["sql"],
        "v2_meta_calls": stats2["meta"] if stats2 else None,
        "v2_hist_calls": stats2["hist"] if stats2 else None,
    })
    return row


rows = []
for view in OBJECTS:
    print(f"measuring {view} ...", flush=True)
    row = measure(view)
    rows.append(row)
    print(f"  v1_cold={row.get('v1_cold_ms')} ms   v2_cold={row.get('v2_cold_ms')} ms", flush=True)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Performance

# COMMAND ----------

import pandas as pd

perf_cols = ["object", "v1_cold_ms", "v2_cold_ms", "v1_warm_ms", "v2_warm_ms",
             "v1_uc_calls", "v1_sql_calls", "v2_meta_calls", "v2_hist_calls",
             "v1_error", "v2_error"]
display(pd.DataFrame(rows)[[c for c in perf_cols if c in pd.DataFrame(rows).columns]])

v1_total = sum(r.get("v1_cold_ms") or 0 for r in rows)
v2_total = sum(r.get("v2_cold_ms") or 0 for r in rows)
print(f"total end-to-end   V1 {v1_total:,.0f} ms    V2 {v2_total:,.0f} ms")
if v2_total:
    print(f"V2 / V1 ratio      {v2_total / v1_total:.2f}x  (>1 means V2 is slower)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. Answers side by side

# COMMAND ----------

ans_cols = ["object", "v1_safe", "v2_safe", "v1_tighter", "v2_sooner",
            "v1_days", "v2_days", "v1_deps", "v2_deps", "v1_skipped", "v2_skipped"]
display(pd.DataFrame(rows)[[c for c in ans_cols if c in pd.DataFrame(rows).columns]])

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. Raw results
# MAGIC
# MAGIC Returned as JSON so a scheduled run can read it from `notebook_output`.

# COMMAND ----------

dbutils.notebook.exit(json.dumps(rows, default=str))
