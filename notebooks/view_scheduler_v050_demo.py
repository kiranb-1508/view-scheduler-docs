# Databricks notebook source
# MAGIC %md
# MAGIC # View Materialization Scheduler `v0.6.0` — New Features Demo
# MAGIC
# MAGIC This notebook showcases and verifies the changes in `view_scheduler 0.6.0`:
# MAGIC
# MAGIC 1. **Spark compute fallback (`v0.5.1` + `v0.6.0`):** `can_refresh("c.s.v")` and `advise("c.s.v")` run directly on the active `SparkSession` when no `WAREHOUSE_ID` is passed.
# MAGIC 2. **Dynamic SQL history window (`since_epoch`):** `DESCRIBE HISTORY` and `event_log()` queries filter by the configured `days_history` window in SQL.
# MAGIC 3. **Three-tier schedule timings (`safe`, `safe_without_outliers`, `usual`):** Outliers appear in `SCHEDULE AT` (worst case), while `WITHOUT OUTLIERS` and `EARLIER` give the schedule without outlier reruns.
# MAGIC 4. **Multi-daily table detection (`multi_daily`, `daily_slots`, `safe_slots`):** Tables that refresh 2+ times on a typical active day are flagged, while 1-off reruns stay normal daily tables.
# MAGIC 5. **Gate cadence and ignore modes (`ignore_non_daily`, `ignore_tables`, `check_multi_daily`, `view_scheduler.mode`):** Weekly, static, event-driven, and multi-daily tables are handled cleanly in `can_refresh()`.

# COMMAND ----------

# MAGIC %pip install /Volumes/workspace/sched_rig/libs/view_scheduler-0.6.0-py3-none-any.whl

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Setup

# COMMAND ----------

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from view_scheduler import Config, Workspace, advise, analyze, can_refresh
from view_scheduler.history import daily_slot_times, looks_multi_daily, summarize
from view_scheduler.models import Commit, TableHistory
from view_scheduler.recommend import recommend
from view_scheduler.report import render

SCHEMA = "workspace.sched_rig"
IST = ZoneInfo("Asia/Kolkata")

# No WAREHOUSE_ID needed inside Databricks: Workspace() uses the active SparkSession!
ws = Workspace()
config = Config()   # days_history=30, buffer_min=15, timezone=Asia/Kolkata, fresh_hours=24

print("config:", config)
print("engine: spark =", ws.spark is not None, "| warehouse_id =", ws.warehouse_id)
summary = {"spark_fallback_active": ws.spark is not None}

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Outlier-Aware Timings (`SCHEDULE AT`, `WITHOUT OUTLIERS`, `EARLIER`)
# MAGIC
# MAGIC First, run `advise` on `t_daily_late_tail` in the live `workspace.sched_rig` schema, and on a 15-day history with 2 outlier rerun days (`06:10`) and 2 heavy-load days (`04:20`).

# COMMAND ----------

live_late = advise(f"{SCHEMA}.t_daily_late_tail", ws, config)
print("Live t_daily_late_tail fields:")
print("  safe_time             :", live_late.safe_time)
print("  safe_without_outliers :", live_late.safe_without_outliers)
print("  earlier_time          :", live_late.earlier_time)
print("  usual_time            :", live_late.usual_time)
print("  outlier_days          :", live_late.outlier_days)
summary["live_late_tail"] = live_late.to_dict()

# COMMAND ----------

# Deterministic 15-day scenario: 11 normal days (04:00), 2 heavy-load days (04:20), 2 outlier reruns (06:10)
base = date(2026, 10, 1)
finishes = {}
for i in range(15):
    d = base + timedelta(days=i)
    if i in (3, 11):
        hh, mm = 6, 10   # 2 outlier rerun days
    elif i in (5, 9):
        hh, mm = 4, 20   # 2 slight load-delay days
    else:
        hh, mm = 4, 0    # 11 normal days
    finishes[d] = datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST)

rec_outliers = recommend([TableHistory("sales.silver.orders", finishes)], buffer_min=15)
print(render("sales.gold.v_orders", rec_outliers, [], "Asia/Kolkata"))
print()
print("safe (with outliers)         :", rec_outliers.safe)
print("safe_without_outliers        :", rec_outliers.safe_without_outliers)
print("usual (no outliers/delays)   :", rec_outliers.usual)
print("outlier_days                 :", [d.isoformat() for d in rec_outliers.outlier_days])

assert str(rec_outliers.safe) == "06:25:00"
assert str(rec_outliers.safe_without_outliers) == "04:35:00"
assert str(rec_outliers.usual) == "04:15:00"
summary["outlier_demo"] = {
    "safe": str(rec_outliers.safe),
    "safe_without_outliers": str(rec_outliers.safe_without_outliers),
    "usual": str(rec_outliers.usual),
    "outlier_days": [d.isoformat() for d in rec_outliers.outlier_days],
}

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Multi-Daily Tables vs. One-Off Reruns
# MAGIC
# MAGIC - `t_multi_daily` in `workspace.sched_rig` refreshes 3 times every day (`02:00`, `09:00`, `17:00`). It is tagged `multi_daily=True` and reports all 3 daily slots and their safe times.
# MAGIC - A daily table that only had 2 commits on 1 or 2 days (due to a manual rerun) keeps `median == 1`, so `multi_daily=False`.

# COMMAND ----------

multi_advice = advise(f"{SCHEMA}.t_multi_daily", ws, config)
dep_multi = multi_advice.dependencies[0]
print("Live t_multi_daily:")
print("  runs        :", dep_multi.runs)
print("  multi_daily :", dep_multi.multi_daily)
print("  daily_slots :", [t.strftime("%H:%M") for t in dep_multi.daily_slots])
print("  safe_slots  :", [t.strftime("%H:%M") for t in dep_multi.safe_slots])
summary["live_multi_daily"] = multi_advice.to_dict()

# COMMAND ----------

# Verify that a 1x/day table with reruns on 2 days out of 10 is NOT flagged as multi_daily
today_d = date(2026, 10, 10)
one_off_commits = []
ver = 1
for i in range(10):
    d = today_d - timedelta(days=i)
    one_off_commits.append(Commit(ver, datetime(d.year, d.month, d.day, 4, 0, tzinfo=IST),
                                  "WRITE", {"mode": "Overwrite"}))
    ver += 1
    if i in (1, 4):  # Only 2 days had a second rerun commit at 06:10
        one_off_commits.append(Commit(ver, datetime(d.year, d.month, d.day, 6, 10, tzinfo=IST),
                                      "WRITE", {"mode": "Overwrite"}))
        ver += 1

one_off_hist = summarize("sales.silver.daily_with_2_reruns", one_off_commits, today_d, 30, IST)
print("1x/day table with 2 rerun days -> multi_daily:", one_off_hist.multi_daily,
      "| daily_slots:", one_off_hist.daily_slots)
assert one_off_hist.multi_daily is False
assert one_off_hist.daily_slots == ()

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Gate (`can_refresh`): Weekly, Static, Event-Driven, and Multi-Daily Options
# MAGIC
# MAGIC ### 3a. Live `v_realistic` with `ignore_non_daily=False` vs `ignore_non_daily=True`
# MAGIC `v_realistic` depends on `t_weekly_mon` (which only writes on Mondays).
# MAGIC - Default (`ignore_non_daily=False`): blocks on Wednesday through Sunday when `t_weekly_mon` is >24h old.
# MAGIC - With `ignore_non_daily=True`: checks `t_weekly_mon` against a 7-day (`168h`) window and passes with `schedule: Mons`.

# COMMAND ----------

print("--- Default (ignore_non_daily=False) ---")
gate_strict = can_refresh(f"{SCHEMA}.v_realistic", ws, config)

print("\n--- Relaxed for weekly/static tables (ignore_non_daily=True) ---")
gate_weekly_ok = can_refresh(f"{SCHEMA}.v_realistic", ws, config, ignore_non_daily=True)

summary["gate_v_realistic_strict"] = gate_strict.ready
summary["gate_v_realistic_ignore_non_daily"] = gate_weekly_ok.ready

# COMMAND ----------

# MAGIC %md
# MAGIC ### 3b. Live `v_broken` with `ignore_tables` and `ignore_non_daily`
# MAGIC `v_broken` reads `t_dormant` (never writes) and `t_maintenance_only`.
# MAGIC Passing `ignore_tables` lets the caller explicitly bypass a known static or event-driven table.

# COMMAND ----------

print("--- Default v_broken (blocks on t_dormant) ---")
gate_broken = can_refresh(f"{SCHEMA}.v_broken", ws, config)
assert gate_broken.ready is False

print("\n--- With ignore_tables=[t_dormant, t_maintenance_only] ---")
gate_ignored = can_refresh(
    f"{SCHEMA}.v_broken",
    ws,
    config,
    ignore_tables=[f"{SCHEMA}.t_dormant", f"{SCHEMA}.t_maintenance_only"],
)
assert gate_ignored.ready is True
summary["gate_v_broken_ignored"] = gate_ignored.ready

# COMMAND ----------

# MAGIC %md
# MAGIC ### 3c. Live `t_multi_daily` with `check_multi_daily=True`
# MAGIC `t_multi_daily` runs 3 times per day (`02:00`, `09:00`, `17:00`).
# MAGIC With `check_multi_daily=True`, the gate shrinks its freshness window from `24h` to `24 // 3 = 8h`.

# COMMAND ----------

gate_multi = can_refresh(f"{SCHEMA}.t_multi_daily", ws, config, check_multi_daily=True)
dep_m = gate_multi.views[0].dependencies[0]
print("t_multi_daily -> cadence:", dep_m.cadence, "| multi_daily:", dep_m.multi_daily,
      "| age_minutes:", dep_m.age_minutes, "| ok:", dep_m.ok)
summary["gate_multi_daily"] = gate_multi.to_dict()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Summary

# COMMAND ----------

print("===== v0.5.0 DEMO SUMMARY =====")
print(json.dumps({
    "live_late_tail_safe": summary["live_late_tail"]["safe_time"],
    "live_late_tail_without_outliers": summary["live_late_tail"]["safe_without_outliers"],
    "outlier_demo": summary["outlier_demo"],
    "live_multi_daily_slots": summary["live_multi_daily"]["dependencies"][0]["daily_slots"],
    "live_multi_daily_safe_slots": summary["live_multi_daily"]["dependencies"][0]["safe_slots"],
    "gate_v_realistic_strict": summary["gate_v_realistic_strict"],
    "gate_v_realistic_ignore_non_daily": summary["gate_v_realistic_ignore_non_daily"],
    "gate_v_broken_ignored": summary["gate_v_broken_ignored"],
}, indent=2))

dbutils.notebook.exit(json.dumps(summary, default=str))
