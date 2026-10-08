# Databricks notebook source
# MAGIC %md
# MAGIC # View Materialization Scheduler — walkthrough
# MAGIC
# MAGIC **The question:** given a Unity Catalog view, what is the earliest time each
# MAGIC day its materialization can run with every dependency already refreshed?
# MAGIC
# MAGIC This notebook runs the library against the fixture views in
# MAGIC `workspace.sched_rig`, which write on a known schedule, so you can check the
# MAGIC answers against something you can reason about.
# MAGIC
# MAGIC > **Read first — the suggested time comes from history.** Nothing checks
# MAGIC > today. If a job runs later than it ever has, work scheduled on this number
# MAGIC > starts anyway and publishes stale data, silently. Treat the output as
# MAGIC > evidence for a decision, not a guarantee. For a live check, use the gate.
# MAGIC
# MAGIC > **The fixture rig is on day 1 of 30.** Every table below currently has one
# MAGIC > or two days of history, and the largest timestamp in the window is the
# MAGIC > *setup* commit from the day the rig was created (~16:43 IST), not a
# MAGIC > scheduled run. So most views here report `SCHEDULE AT 16:58` — pinned by setup,
# MAGIC > not by the pipeline. That is a real property of the current data, not a
# MAGIC > bug, and it is worth seeing: it shows exactly how a single unrepresentative
# MAGIC > commit drags the recommendation, and why the `DAYS` column and the
# MAGIC > `WHEN THE LAST JOB FINISHED` block matter more than the headline time when
# MAGIC > evidence is thin. Re-run this in a week for a realistic picture.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Install
# MAGIC
# MAGIC The wheel lives in a Unity Catalog Volume, so access is governed by
# MAGIC `GRANT READ VOLUME` — no GitHub account or token needed.

# COMMAND ----------

# MAGIC %pip install /Volumes/workspace/sched_rig/libs/view_scheduler-0.3.0-py3-none-any.whl

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Point it at a warehouse
# MAGIC
# MAGIC History reads go through the SQL Statement Execution API, so the library
# MAGIC needs a warehouse. It needs `SELECT` on each dependency and `USE` on the
# MAGIC catalogs and schemas involved.

# COMMAND ----------

from view_scheduler import Config, Workspace, analyze

WAREHOUSE_ID = "f96bd3430e175166"   # ← your SQL warehouse
SCHEMA = "workspace.sched_rig"

ws = Workspace(warehouse_id=WAREHOUSE_ID)
config = Config()      # days_history=30, buffer_min=15, timezone=Asia/Kolkata
print(config)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. The simplest case
# MAGIC
# MAGIC `v_healthy` sits on two tables that both refresh daily in the 04:00 slot.
# MAGIC This is what a well-behaved view looks like.

# COMMAND ----------

text, histories, skipped = analyze(f"{SCHEMA}.v_healthy", ws, config)
print(text)

# COMMAND ----------

# MAGIC %md
# MAGIC ### How to read that
# MAGIC
# MAGIC | Column | Meaning |
# MAGIC |---|---|
# MAGIC | `RUNS` | Which weekdays it was seen running — observed, not declared |
# MAGIC | `USUAL` | Median finish time |
# MAGIC | `LATEST` | Latest finish **time of day** — often not the most recent date |
# MAGIC | `DAYS` | How many days it actually refreshed in the window |
# MAGIC | `MISSED` | Days it was expected to run and didn't |
# MAGIC
# MAGIC `SCHEDULE AT` is the latest finish in the period plus a small gap. It would
# MAGIC have worked on every day observed. `EARLIER` is the second latest: a bit
# MAGIC sooner, but one job had not finished yet on one day. **Starting early means
# MAGIC publishing stale data**, so prefer the safe time unless you have a reason.
# MAGIC
# MAGIC The `WHEN THE LAST JOB FINISHED` block is the evidence behind those two times —
# MAGIC it names the day that set each one, so a surprising answer can be traced to
# MAGIC the day that caused it rather than taken on faith.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. A view with a weekly dependency
# MAGIC
# MAGIC `v_realistic` includes `t_weekly_mon`, which only runs on Mondays, and
# MAGIC `t_flaky`, which skips days. Watch the warnings.

# COMMAND ----------

text, histories, skipped = analyze(f"{SCHEMA}.v_realistic", ws, config)
print(text)

# COMMAND ----------

# MAGIC %md
# MAGIC A weekly dependency is not a scheduling problem you can solve by picking a
# MAGIC different time — on Tuesday it simply will not be fresh. The report says so
# MAGIC rather than folding it into the number and hoping you notice.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Thin evidence, and how to spot it
# MAGIC
# MAGIC `v_broken` depends on `t_dormant` (written once at setup, never again) and
# MAGIC `t_maintenance_only` (a real write every third day, metadata-only commits
# MAGIC otherwise).
# MAGIC
# MAGIC On day 30 `t_dormant` will stand out as a table that refreshed once in
# MAGIC thirty days. **Today it does not**, because that one setup commit is the
# MAGIC entire window — so it reports `Mons` with `DAYS 1` and `MISSED 0`, and the
# MAGIC view gets a confident-looking `safe`. Watch what actually gives it away.

# COMMAND ----------

text, histories, skipped = analyze(f"{SCHEMA}.v_broken", ws, config)
print(text)

# COMMAND ----------

# MAGIC %md
# MAGIC ### What gives it away is `DAYS`, not the headline
# MAGIC
# MAGIC `MISSED 0` is technically true and completely misleading — a table cannot
# MAGIC miss days it was never expected to run. The signals that matter:
# MAGIC
# MAGIC * **`DAYS 1`** — one day of evidence behind that time.
# MAGIC * **`runs only on Mons`** — the pattern was inferred from a single
# MAGIC   observation that happened to fall on a Monday.
# MAGIC * **the weak-advice warning**, which names the dependency with the fewest days.
# MAGIC * **`WHEN THE LAST JOB FINISHED`**, showing the single day the time rests on.
# MAGIC
# MAGIC Read `MISSED` next to `DAYS` or you will read it wrong. This is the honest
# MAGIC failure mode of a 30-day statistic computed on day 1, and the report's job is
# MAGIC to make it visible rather than hide it behind a plausible time.

# COMMAND ----------

# MAGIC %md
# MAGIC ### What an outright refusal looks like
# MAGIC
# MAGIC When no single day has *every* dependency refreshing, there is no honest
# MAGIC answer and the tool declines to invent one. The rig cannot show this today,
# MAGIC so here it is against the pure API — which needs no warehouse at all, and is
# MAGIC how you would unit-test against this library.

# COMMAND ----------

from datetime import date, datetime, timezone

from view_scheduler.models import TableHistory
from view_scheduler.recommend import recommend
from view_scheduler.report import render

def at(day, hour):
    return datetime(2026, 8, day, hour, 0, tzinfo=timezone.utc)

# Two tables that never refresh on the same day: no intersection, no answer.
monday_only = TableHistory("bronze.orders",    {date(2026, 8, 3): at(3, 4)})
friday_only = TableHistory("bronze.customers", {date(2026, 8, 7): at(7, 4)})

print(render("gold.no_overlap", recommend([monday_only, friday_only], 15), [], "UTC"))

# COMMAND ----------

# MAGIC %md
# MAGIC A tool that invented a plausible time here would be worse than useless.
# MAGIC Note also that `OPTIMIZE` / `VACUUM` / `SET TBLPROPERTIES` are maintenance,
# MAGIC never refreshes, so a table that only ever sees those reads as dormant —
# MAGIC which is the truth.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. Midnight, and why the day boundary is pinned
# MAGIC
# MAGIC `v_midnight` is built to straddle the boundary: `t_near_midnight` writes at
# MAGIC 00:20 IST and `t_late_evening` at 23:40. In UTC those land on *different
# MAGIC calendar days*, which is the bug that pinning the timezone prevents.
# MAGIC
# MAGIC Once a few days of slot writes accumulate you will see those two times sit
# MAGIC either side of the boundary. Today the window is still dominated by the
# MAGIC setup commit, so the effect is not visible yet — run this again in a week.

# COMMAND ----------

text, histories, skipped = analyze(f"{SCHEMA}.v_midnight", ws, config)
print(text)

# COMMAND ----------

# MAGIC %md
# MAGIC ### The rollover, shown directly
# MAGIC
# MAGIC If the buffer pushes the start past midnight, a bare clock time reads as
# MAGIC roughly 24 hours early — schedule `00:05` same-day and the job runs *before*
# MAGIC every dependency. The report marks it `(+1 day)` and warns:

# COMMAND ----------

late = TableHistory("bronze.nightly", {
    date(2026, 8, d): at(d, 23).replace(minute=50) for d in (3, 4, 5, 6, 7)
})
result = recommend([late], buffer_min=15)

print(f"safe={result.safe}   crosses midnight={result.safe_next_day}\n")
print(render("gold.after_midnight", result, [], "UTC"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. Nested views
# MAGIC
# MAGIC `v_deep` is a view over two other views. The walker recurses to the physical
# MAGIC tables underneath — views themselves have no commit history, so only the
# MAGIC tables gate the schedule. A table reached by two paths is read once.

# COMMAND ----------

from view_scheduler.walker import walk

gating, walk_skipped = walk(f"{SCHEMA}.v_deep", ws.get_table)
print(f"v_deep gates on {len(gating)} physical tables:")
for name in gating:
    print("   ", name)
if walk_skipped:
    print("\nskipped:")
    for item in walk_skipped:
        print(f"    {item.name} — {item.reason}")

# COMMAND ----------

text, _, _ = analyze(f"{SCHEMA}.v_deep", ws, config)
print(text)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 8. Using the return values, not the text
# MAGIC
# MAGIC `analyze` returns `(text, histories, skipped)`. The text is for humans; the
# MAGIC other two are for code — building a table of recommendations, alerting on
# MAGIC drift, or feeding a dashboard.

# COMMAND ----------

from view_scheduler.recommend import recommend

views = ["v_healthy", "v_realistic", "v_midnight", "v_deep"]
rows = []

for view in views:
    fqn = f"{SCHEMA}.{view}"
    try:
        _, histories, skipped = analyze(fqn, ws, config)
        result = recommend(histories, config.buffer_min)
        rows.append({
            "view": view,
            "safe": str(result.safe) if result.safe else None,
            "earlier": str(result.earlier) if result.earlier else None,
            "next_day": result.safe_next_day,
            "days_of_evidence": result.n_days,
            "dependencies": len(result.rows),
            "skipped": len(skipped),
        })
    except Exception as exc:
        rows.append({"view": view, "safe": None, "error": str(exc)[:80]})

display(spark.createDataFrame(rows))

# COMMAND ----------

# MAGIC %md
# MAGIC ### Persisting this is how you get drift detection
# MAGIC
# MAGIC `safe` is a rolling statistic over the last 30 days, but it tends to get
# MAGIC copied into a cron once and left there. Write this table on a schedule and
# MAGIC compare against last week's row: when an upstream team moves their load, the
# MAGIC recommendation moves and you find out. That is the cheapest protection
# MAGIC available while there is no runtime gate.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 9. Nothing is dropped silently
# MAGIC
# MAGIC Anything unreadable — a foreign table, a permission failure, a materialized
# MAGIC view whose pipeline event log you cannot see — comes back in `skipped` with a
# MAGIC reason, and appears in the report. A partial answer that names its gaps beats
# MAGIC a confident answer built on missing data.

# COMMAND ----------

_, _, skipped = analyze(f"{SCHEMA}.v_realistic", ws, config)
print(f"skipped: {len(skipped)}")
for item in skipped:
    print(f"  {item.name} — {item.reason}")

# Deliberately unreadable: a view that does not exist.
_, _, skipped = analyze(f"{SCHEMA}.does_not_exist", ws, config)
print("\nagainst a non-existent view:")
for item in skipped:
    print(f"  {item.name} — {item.reason}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 10. Tuning
# MAGIC
# MAGIC Three settings, and there is a standing rule to ask before adding a fourth.

# COMMAND ----------

wider = Config(days_history=60, buffer_min=30, timezone="Asia/Kolkata")
text, _, _ = analyze(f"{SCHEMA}.v_healthy", ws, wider)
print(text)

# COMMAND ----------

# MAGIC %md
# MAGIC A longer window gives more evidence but is bounded by
# MAGIC `delta.logRetentionDuration` — commits age out of `DESCRIBE HISTORY`
# MAGIC (default 30 days), so asking for 60 does not conjure history that Delta has
# MAGIC already dropped.
# MAGIC
# MAGIC **Known limitation:** the window is a flat cutoff applied to every table. A
# MAGIC dependency with short retention reports a large `MISSED` count that looks
# MAGIC like a reliability problem but is only a shorter history. Read `MISSED`
# MAGIC alongside `DAYS` before chasing it.

# COMMAND ----------

# MAGIC %md
# MAGIC ## From the command line
# MAGIC
# MAGIC The same thing without a notebook:
# MAGIC
# MAGIC ```bash
# MAGIC pip install /Volumes/workspace/sched_rig/libs/view_scheduler-0.3.0-py3-none-any.whl
# MAGIC export WAREHOUSE_ID=f96bd3430e175166
# MAGIC view-scheduler workspace.sched_rig.v_realistic
# MAGIC view-scheduler workspace.sched_rig.v_healthy --days 60 --buffer 30 --quiet
# MAGIC ```
