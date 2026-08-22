# Genie Code build prompt

Part A goes in Genie Code **User instructions** (⚙️ → User instructions) once.
Part B prompts go in the chat, one at a time, waiting at each gate.

Before starting: turn auto-approve **off** (⚙️ → Actions → *Ask first*), set effort
to **Auto**, work in a Git folder, and use `@` to reference real views/tables at
each verification step.

---

## Part A — User instructions

```
Project: View Materialization Scheduler (Databricks accelerator).

GOAL
A BI user hands us a Unity Catalog view and asks "what's the earliest time each
day I can schedule its materialization?" (CREATE OR REPLACE TABLE t AS SELECT *
FROM view). Answer with a clock time plus the evidence.

HARD RULES
- READ ONLY. Never write, alter, optimize, vacuum, or drop any user table.
  Metadata and history reads only. No exceptions, ever.
- No SQL parsing of view definitions.
- No environment changes. Never require altering delta.logRetentionDuration or
  any table property.
- Exactly three config parameters: days_history=30, buffer_min=15,
  timezone="Asia/Kolkata". Ask before adding a fourth.
- Output must be readable by a junior engineer at 3am.

DOMAIN FACTS — established, do not rediscover or "improve"
1. Dependencies come from the UC Tables API: GET /api/2.1/unity-catalog/tables/
   {full_name}, field view_dependencies. NOT system.access.table_lineage — a new
   view has never run so lineage is empty. NOT SQL parsing.
2. view_dependencies is NOT returned by listTables. One call per object. Memoize.
3. Refresh detection uses DESCRIBE HISTORY, NOT system.lakeflow.*. Upstream is a
   mix of Azure Data Factory, Databricks jobs, and Synapse Link; ADF and Synapse
   never appear in Databricks job system tables. Delta commit history is the only
   uniform signal.
4. Operation class comes from operationParameters.mode, NOT the operation name.
   The commonest name is plain "WRITE"; mode=Overwrite is full replace,
   mode=Append is incremental. Classifying by name marks most tables dormant —
   a silent wrong answer, not an error.
5. A full-replace commit qualifies regardless of row count. A recompute that
   produced zero rows still ran.
6. If operationMetrics is null/empty, treat the commit as qualifying and flag the
   table "metrics-degraded". ADF and Synapse write through non-Spark paths and
   may emit sparse metrics. Without this, live tables read as dormant.
7. DESCRIBE HISTORY does NOT contain partition values written by a commit.
   Build no partition-based logic.
8. Databricks TIMESTAMP renders in spark.sql.session.timeZone (default UTC). Pin
   it explicitly or the same view returns different times on different warehouses.
9. History retention is delta.logRetentionDuration (default 30d). VACUUM does NOT
   truncate DESCRIBE HISTORY. Derive each table's real window from its oldest
   entry; never assume 30 days.
10. A day counts only if EVERY dependency refreshed on it. Days where something
    didn't run are excluded from timing and reported in a separate "missed"
    column — a table that misses days is a reliability problem no schedule fixes.

DO NOT BUILD
No pre-flight gate, cadence classifier, confidence tiers, coverage percentages,
slot model, changepoint detection, caching layer, or state store. A prior design
had all of them and they were deleted as unusable. If you think one is needed,
stop and ask.

ENGINEERING STANDARDS
- Code lives in workspace files as .py modules, not inline in notebooks. One thin
  notebook is the runner. This keeps it testable and reviewable.
- Separate pure functions from I/O. All computation over commit data must be
  testable with no Spark session and no network.
- Use databricks-sdk WorkspaceClient() — it auto-authenticates in-workspace.
  No tokens, no secrets in code.
- Type hints throughout. Structured logging: one line per external call with
  target and elapsed ms.
- Deterministic. Pure functions never read the wall clock — pass `today` in.
- Never silently drop a dependency. Anything unresolved (permission, non-Delta,
  foreign, function) goes into a named `skipped` list with a reason and appears
  in the report.
- Retry with exponential backoff on 429/5xx, REST calls only.

WORKING STYLE
Work one phase at a time. Stop at each verification gate, show me the output, and
wait for my go-ahead before continuing. Do not run ahead.
```

---

## Part B — phase prompts

### Phase 0
```
Phase 0 — scaffold. Create a Git folder project with .py modules in workspace
files plus one runner notebook. Set up the config object, structured logging, and
a fixtures module producing fake DESCRIBE HISTORY rows and fake
view_dependencies responses. Add pytest.

GATE: run pytest, show me the tree and the fixture shapes. Then stop.
```

### Phase 1
```
Phase 1 — dependency walker. walk(fqn) -> (gating_tables, skipped). Recursive,
memoized on full name, visited-set cycle guard, depth cap 60. VIEW passes through
and recurses; MANAGED/EXTERNAL/MATERIALIZED_VIEW/STREAMING_TABLE gate;
FOREIGN, HMS and function dependencies go to skipped with a reason.

GATE: unit tests with a mocked API covering nested views, a diamond (shared
dependency visited exactly once), a cycle, a FOREIGN table, a function
dependency, and a 403 on one node that must NOT abort the walk. Then run it
against @<your real view> and show both lists. Then stop.
```

### Phase 2
```
Phase 2 — history reader. qualifying(row) -> bool per the operation rules in my
instructions, and finish_times(table, days) -> {date: timestamp} taking the first
qualifying commit of each day. Derive each table's real history window from its
oldest entry.

GATE: unit tests over fixtures for WRITE+Overwrite, WRITE+Append, MERGE, UPDATE,
a zero-row full replace, null operationMetrics, OPTIMIZE, VACUUM, DELETE. Then
run against one real table per writer: @<an ADF-fed table>, @<a Databricks
job-fed table>, @<a Synapse Link table>. Show me the classified commits for all
three and confirm none read as dormant. Then stop.

This phase is the one most likely to be silently wrong. Do not proceed on unit
tests alone.
```

### Phase 3
```
Phase 3 — roll-up and recommendation. recommend(tables, buffer_min) ->
(safe, tighter, n_days). Intersect days so only cycles where EVERY dependency
refreshed are counted. all_clear per day = max across tables. safe = worst +
buffer, tighter = second-worst + buffer.

GATE: unit tests with hand-built timestamps, including exactly this case:
  orders:     Mon 04:00  Tue 04:10  Wed 04:05  Thu 05:30  Fri 04:00
  customers:  Mon 03:50  Tue 03:45  Wed 06:10  Thu 03:50  Fri 03:55
  => all_clear: 04:00, 04:10, 06:10, 05:30, 04:00
  => safe = 06:25, tighter = 05:45  (buffer 15)
Then run end to end on @<your real view>. Then stop.
```

### Phase 4
```
Phase 4 — report. Render: the two times, then one row per dependency (name,
refreshes, usual, worst, missed), then warnings. Warnings print ONLY when they
apply — no applicable warnings means that section is absent entirely, not empty.

GATE: show the report for @<your real view>, and for a view with a deliberately
unreadable dependency, to confirm the skipped branch surfaces. Then stop.
```

### Phase 5 — start a NEW chat
```
Phase 5 — impact analysis. SEPARATE module, separate entry point. Given a table,
find what depends on it.

This is NOT the Phase 1 walker reversed — view_dependencies has no reverse index.
Source is system.access.table_lineage. Recursive CTE, filter on event_date FIRST
(it is the partition column), cap at 6 hops, dedupe, cycle guard. Group results
by hop count and entity type so dashboards, jobs and queries are visible
alongside tables.

GATE: run against @<a table whose consumers you already know>. Confirm the direct
consumers match reality before trusting anything transitive. Then stop.
```
