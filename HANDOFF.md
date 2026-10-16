# HANDOFF — rebuild the View Scheduler from these requirements

Status: requirements and decisions are final unless the owner says otherwise.
Date: 2026-10-08.
Audience: a new engineer or agent rebuilding this project from scratch.

Read this whole document before you write code. It contains the exact
requirements, the hard constraints, and the mistakes that were already found
and fixed. Do not rediscover them.

---

## 0. How to use this document

1. Read section 1 and 3 first. They define the job.
2. Obey section 7 (hard constraints) at all times.
3. Do not relitigate section 11 (decisions already made).
4. Read section 14 before you write the walker or the report. Those are real
   defects that shipped once.
5. Use section 17 to decide when you are done.

---

## 1. The problem

A scheduled job refreshes a Unity Catalog view at a fixed time. An upstream
dependency may not have refreshed yet. The view refresh runs anyway and
publishes stale data. Nothing errors. Nobody is told.

Write that sentence down. It is the whole point:

> A confident wrong answer, not an error.

Every requirement below exists to make that failure loud instead of silent.

---

## 2. The two deliverables

One small Python library. Two tools.

| Tool | Question | Type |
|---|---|---|
| `analyze(view)` | When each day can this view run, from history? | Advisory. Predicts. |
| `can_refresh(view or [views])` | Right now, is every dependency fresh? | The gate. Enforces. |

The gate is the product. The advisory tool may ship beside it.

---

## 3. The gate: exact behaviour

Given a view, or a list of views, walk each view down to the physical tables it
reads. For each physical table, ask one question: did it write recently?

Return `READY` only when every dependency of every view is fresh.

### The decision table. Implement all of it.

| Condition | Verdict |
|---|---|
| Wrote inside the window | fresh |
| Wrote before the window | stale — **blocks the view** |
| No qualifying write anywhere in its history | **blocks** |
| Cannot be read — missing, no permission, any error | **blocks** |
| Streaming table | check its Delta history like any table — one on a schedule is stale between runs (revised 2026-10-09; it was "always fresh") |
| Materialized view | not a Delta table; read its pipeline event log |
| Function dependency (a SQL UDF in the view) | a **note**, never blocks |
| Dependency cycle | **blocks** |
| Depth cap exceeded | **blocks** |
| View reports no dependencies at all | **blocks** (fail closed) |
| Dependency is a plain managed/external table | it is a leaf; check it normally |
| A plain table is passed as the root | it is its own dependency; check it |
| The same view appears twice in the list | report it twice |

### The window

- Rolling hours. **Not** a calendar day.
- Default **24 hours**.
- One value for the whole call. Not per-view.
- Lives in `Config.fresh_hours`. Overridable per call.
- `fresh_hours <= 0` is an error.
- Reference time is `now()` in the config timezone. Allow an explicit override
  so tests are deterministic.
- A write dated **after** the reference time is ignored. A clock-skewed future
  commit must not make a stale table look fresh.

### The report

- Print one block per view.
- One overall flag for the whole call.
- Every dependency gets a line, with its age. Not only the blockers.
- Every blocker names the dependency **and** the view that needs it.
- End with a plain, actionable line: what to refresh, and for whom.

### What the gate must never do

- Never trigger a refresh.
- Never write. Read-only, always.
- Never raise for a dependency problem. Record it and block.

---

## 4. The gate: exact API

```python
from view_scheduler import can_refresh

result = can_refresh("catalog.schema.my_view")     # WAREHOUSE_ID from the env
if result.ready:
    ...run the refresh...
else:
    print(result)
```

```python
def can_refresh(
    views: str | Sequence[str],
    ws: Workspace | None = None,        # default: build from WAREHOUSE_ID
    config: Config | None = None,       # default: Config()
    *,
    now: datetime | None = None,        # default: now() in the config timezone
    fresh_hours: int | None = None,     # default: config.fresh_hours
    quiet: bool = False,                # default: print the report
) -> GateResult: ...
```

Public result types. Keep the count small.

```python
@dataclass(frozen=True)
class Dependency:
    name: str
    kind: str                 # MANAGED / MATERIALIZED_VIEW / STREAMING_TABLE / FUNCTION / UNKNOWN
    ok: bool                  # False blocks the view
    last_refresh: datetime | None = None
    age_minutes: int | None = None
    detail: str = ""          # human reason; shown when not ok, or for a note


@dataclass(frozen=True)
class ViewResult:
    view: str
    ready: bool
    dependencies: tuple[Dependency, ...] = ()
    # ready == all(d.ok for d in dependencies)


@dataclass(frozen=True)
class GateResult:
    ready: bool                # all views ready
    views: tuple[ViewResult, ...]
    fresh_hours: int
    timezone: str
    # blockers() -> tuple of dependency names, in order, without repeats
    # to_dict()  -> JSON-ready
    # render() / __str__ -> the report
```

There is **one** list of dependencies per view. Everything the walk found —
measured tables, cycles, unreadable objects, function notes — becomes a
`Dependency` with `ok` set. Do not build three channels.

---

## 5. The gate: exact output

This exact text is pinned by a golden test. Match it.

```
GATE  BLOCKED   1 view, window 24h, times UTC

  BLOCKED c.s.v
      stale  c.s.t_old  (MANAGED)  last refresh 2026-08-04 04:00 UTC (2d 8h ago) — older than the 24h window
      fresh  c.s.t_fresh  (MANAGED)  last refresh 2026-08-06 04:00 UTC (8h 00m ago)

To unblock, refresh:
  c.s.t_old   (needed by c.s.v)
```

Rules for the format:

- Header line: `GATE  READY|BLOCKED   <n> view(s), window <N>h, times <tz>`.
- View label padded to 8 characters, so `READY` and `BLOCKED` align.
- Dependencies sorted with the stale ones first.
- A dependency with no timestamp and `ok=True` (a streaming table) prints as
  `note`, not `fresh`, and keeps its reason visible.
- Ages are human: `45m`, `11h 48m`, `2d 8h`.
- Timestamps are rendered in the config timezone, with the zone abbreviation.

The output must be readable at 3am by a junior engineer. Use plain words.
Never use jargon like "window" without saying the number.

---

## 6. The advisory tool

Suggest the time each day a view can run with every upstream job already
finished. It reads history. It is a prediction, not a check.

```python
from view_scheduler import advise

advice = advise("sales.gold.customer_360")   # WAREHOUSE_ID from the env
advice.safe_time      # datetime.time(6, 25)  or None
advice.earlier_time   # datetime.time(4, 25)  or None
advice.next_day       # True when the (+1 day) marker applies
advice.dependencies   # one row per upstream job
advice.to_dict()      # JSON
print(advice)         # the report
```

`advise()` prints the report and returns an `Advice`. `analyze()` is the
low-level call: it returns a tuple `(text, histories, skipped)`.

It is **not** the gate. Do not merge them. `advise` predicts. `can_refresh`
checks.

### Wording rule for this tool

Plain words only. The two times are called **safe** and **earlier**. Do not use
"tighter", "all-clear", or "advisory" anywhere a user can see. The table
columns are `RUNS / USUAL / LATEST / DAYS / MISSED`. The evidence block is
`WHEN THE LAST JOB FINISHED, DAY BY DAY`. Say "job" or "table" where you can.

---

## 7. HARD CONSTRAINTS — never violate these

1. **Read-only.** The package never writes, alters, optimizes, vacuums, drops,
   or refreshes anything. Metadata and history reads only. If a change would
   write to the user's data, stop and ask.

2. **Fail closed.** Prefer a false BLOCK over a false READY, every time. A
   dependency we cannot read blocks the view.

3. **Never write the owner's work email address anywhere.** Not in code, tests,
   docs, notebooks, paths, commits, PR titles, PR bodies, or terminal output.
   It must not appear in the repository or on GitHub.
   - Commit identity is `KIRAN B <kiranlillybala@gmail.com>`.
   - Databricks notebook paths sit under the account's home folder. Resolve the
     path at runtime from the API (`GET /api/2.0/preview/scim/v2/Me`, field
     `userName`). Never hardcode it, never print it. Redact it in any echoed
     command.

4. **Writing style is ASD-STE 100.** Short sentences. Active voice. One idea
   per sentence. Simple words. This applies to code comments, docstrings,
   README, PR text, and every reply to the owner.

5. **Minimal change to the existing library.** Reuse the existing package and
   its I/O seam. Do not add a second I/O path. Do not introduce a new I/O
   library.

6. **Reuse the existing seam.** Metadata and history read through one small
   `Workspace` object, so the walk and the gate are testable with no network.

7. **The owner's Databricks access is always available** through user-level
   environment variables. Use them. Do not change them.

8. **Do not break fixture determinism.** See section 9. Do not run a rig job
   out of its time slot. It writes an out-of-window commit and corrupts the
   fixture.

---

## 8. Environment and infrastructure

| Item | Value |
|---|---|
| Databricks host | `https://dbc-11b23bdc-0b02.cloud.databricks.com` |
| Auth | user-level env vars `DATABRICKS_HOST`, `DATABRICKS_TOKEN` |
| SQL warehouse | `f96bd3430e175166` (serverless, 2X-Small, auto-start) |
| Catalog and schema | `workspace.sched_rig` |
| Library volume | `/Volumes/workspace/sched_rig/libs/` |
| Repository | `https://github.com/kiranb-1508/view-scheduler-docs` |

Notes that cost time to learn:

- The CLI is **Databricks CLI v1.14.1**. `jobs run-now`, `jobs get-run`, and
  `jobs get-run-output` take **positional** IDs, not `--job-id` / `--run-id`.
- A one-time serverless run works through `jobs submit` with
  `environments: [{environment_key: "default", spec: {client: "3"}}]`.
- The notebook-exit payload is limited. Keep the JSON small.

---

## 9. The fixture rig

The rig writes real, multi-day commit history into `workspace.sched_rig`. It
exists so the gate and the analyzer are validated against real data, not mocks.

| Item | Value |
|---|---|
| Bundle | `sched_rig`, target `dev` |
| Jobs | 7, named `[dev kiran_b] sched_rig HH:MM IST` |
| Slots (Asia/Kolkata) | 00:20, 02:00, 04:00, 06:10, 09:00, 17:00, 23:40 |
| Window start | 2026-09-01 |
| Window end | 2026-10-31 |
| Repo copy | `rig/plan.py`, `rig/databricks.yml`, `rig/write_slot.py` |

Objects in the schema (22 total):

- 13 `MANAGED` tables
- `t_dormant` — never writes
- `t_maintenance_only` — writes only maintenance operations on some days
- `t_weekly_mon` — writes on Mondays only
- `t_append_zero`, `t_overwrite_zero` — zero-row writes
- 7 `VIEW`s: `v_healthy`, `v_realistic`, `v_midnight`, `v_broken`, `v_deep`,
  `v_deep_left`, `v_deep_right`
- `mv_daily` — `MATERIALIZED_VIEW`
- `st_stream` — `STREAMING_TABLE`

Confirmed dependency sets (verified against the live schema):

| View | Dependencies |
|---|---|
| `v_healthy` | `t_daily_append`, `t_daily_tight` |
| `v_realistic` | `t_daily_late_tail`, `t_daily_tight`, `t_flaky`, `t_weekly_mon` |
| `v_midnight` | `t_late_evening`, `t_near_midnight` |
| `v_broken` | `t_dormant`, `t_maintenance_only` |
| `v_deep` | `v_deep_left`, `v_deep_right` → resolves to `t_daily_tight`, `t_merge` |

**Fixture determinism rule.** Each slot writes on a schedule. If you trigger a
job outside its slot, it writes a commit the fixtures do not expect, and every
downstream check becomes wrong. Do not manually run a rig job to "test
something". Use a throwaway probe job instead.

**Self-pause.** `rig/write_slot.py` pauses all 7 rig jobs when the date passes
the window end. This is intended.

**Keep the history generating.** The owner wants the jobs running and scheduled
through end of October, so the gate sees live, moving data. The demo notebook
must be **excluded** from the rig schedule.

---

## 10. Domain facts you must not get wrong

These were verified against a live workspace. They are not opinions.

1. **`view_dependencies` is nested.** The name sits one level down:
   `dependency["table"]["table_full_name"]`. Reading
   `dependency["table_full_name"]` finds nothing and silently treats every
   dependency as a function dependency. That bug shipped once.

2. **Operation class comes from `operationParameters.mode`, not the operation
   name.** The commonest Delta operation is plain `WRITE`. Only its mode says
   whether it replaced or appended. Classify by name alone and most tables look
   dormant — a confident wrong answer.

3. **Read timestamps as epochs, not rendered strings.**
   `SELECT unix_timestamp(timestamp)` returns the instant. A rendered
   `TIMESTAMP` string depends on the warehouse session timezone, which this
   tool does not control. `spark.sql(...).collect()` in a notebook returns a
   **naive UTC** datetime; the session timezone does not change it. Stamping a
   naive value with the config zone is wrong by the UTC offset. That bug
   shipped once too.

4. **A materialized view rejects `DESCRIBE HISTORY`.** Read its completed
   refreshes from `event_log(TABLE(<mv>))` where `event_type = 'update_progress'`
   and the state is `COMPLETED`.

5. **Streaming tables are Delta tables and do have history.** Read that
   history and check it like any table. A streaming table on a schedule
   commits once per run, so between runs it is stale, and a paused one is
   stale the whole time it is paused. Revised 2026-10-09. The first rule was
   "treat them as always fresh; do not read their history". The rig's own
   `st_stream` then went 24 days without a commit (2026-09-13 to 2026-10-07,
   verified live), and the gate called it fresh through all of them. That is
   the false READY this project exists to prevent, so the owner reversed the
   decision. Refreshes appear as `STREAMING UPDATE` commits, which the
   classifier already counts as incremental.

6. **`DESCRIBE HISTORY` carries no partition values.** Do not try to attribute
   commits to business dates by partition.

7. **Quote every object name.** A name with a hyphen is subtraction unless it
   is quoted. Quote per dot-separated part, and double any inner backtick.
   Percent-encode the metadata URL path.

8. **The SQL Statement API paginates.** A large result arrives in chunks. Read
   them all, and refuse a result whose manifest says it is truncated. Do not
   silently use the first page.

9. **`operationMetrics` is a wide JSON column.** The gate classifies commits
   only, so it does not need it. Do not fetch it for the gate.

10. **A fixed daily window blocks a Mon-Fri table every Monday.** A table that
    writes Monday to Friday last wrote on Friday. On Monday morning that write
    is about 74 hours old, so a 24-hour window blocks the view — every Monday,
    forever. The gate now checks each table against its own observed schedule.
    The window is one plus the longest gap between run weekdays, times
    `fresh_hours`: a Mon-Fri table gets 96 hours and a Mondays-only table gets
    192. The gate extends the window only for a trusted table — at least ten
    run days, and no weekday seen just once. A thin history keeps the daily
    window, so a new table cannot relax its own check. A missed run is still
    caught within one day. Revised 2026-10-16.

11. **Classify a commit exactly like this, in this order:**

    | Step | Rule | Class |
    |---|---|---|
    | 1 | operation in `IGNORE` | not a refresh |
    | 2 | operation in `FULL_REPLACE`, or (`WRITE` and mode `Overwrite`) | refresh |
    | 3 | operation in `INCREMENTAL`, or (`WRITE` and mode `Append`) | refresh |
    | 4 | operation is `WRITE` | refresh |
    | 5 | operation is `MV REFRESH` | refresh |
    | 6 | anything else | not a refresh |

    - `FULL_REPLACE` = CREATE OR REPLACE TABLE, CREATE OR REPLACE TABLE AS
      SELECT, CREATE TABLE AS SELECT, REPLACE TABLE AS SELECT, INSERT OVERWRITE,
      TRUNCATE AND INSERT.
    - `INCREMENTAL` = MERGE, UPDATE, COPY INTO, STREAMING UPDATE, INSERT.
    - `IGNORE` = OPTIMIZE, VACUUM, VACUUM START, VACUUM END, DELETE, TRUNCATE,
      SET/UNSET TBLPROPERTIES, CHANGE COLUMN, ADD COLUMNS, RESTORE, CLONE,
      constraints, LIQUID CLUSTERING, RENAME COLUMN, UPGRADE PROTOCOL, SET
      COLUMN COMMENT.
    - **A zero-row write still counts as a refresh.** A full replace that
      produced no rows still ran. So did a zero-row append.

---

## 11. Decisions already made — do not relitigate

The owner was interviewed in depth. These are settled:

| Question | Decision |
|---|---|
| Base code | The existing V1 package, not a rewrite |
| I/O | Reuse the existing `Workspace` seam |
| Freshness | Rolling hours, not a calendar day |
| Window default | 24 hours for a daily table |
| Window scope | One `fresh_hours` per call. Each table earns its own window from its observed schedule, under a fail-closed trust guard. (reversed 2026-10-16; was one window for every table) |
| Cadence checking | On by default. `ignore_non_daily` now covers only a table with no write in history. (reversed 2026-10-16; cadence checking was opt-in) |
| What qualifies as a write | The V1 rule above, including zero rows |
| Walk depth | Recurse views, stop at tables and MVs |
| Streaming | Checked from history (reversed 2026-10-09; was "always fresh") |
| Materialized view | Pipeline event log |
| Missing or unreadable | Block |
| Function dependency | A note, not a blocker |
| Action | Decide only; never refresh |
| Output | Structured result **and** printed report |
| Result detail | Per-view detail plus one overall flag |
| Blocker content | Dependency **and** the view that needs it |
| Ages | For every dependency |
| CLI subcommand | Not required |
| Module and function names | `gate.py`, `can_refresh()` |
| Clock | `now()` in the config zone, overridable for tests |

---

## 12. Testing requirements

- **All tests run offline.** No network, no warehouse, no Spark, no
  credentials. Inject a fake `Workspace`.
- Pin the clock. Pass `now=` to `can_refresh` in every test.
- The test suite must cover at least:

  | Case | Expected |
  |---|---|
  | All dependencies fresh | READY |
  | One dependency stale | BLOCKED; the blocker names the isolated table |
  | Zero-row append | fresh |
  | Maintenance-only history | stale |
  | Newest **qualifying** write wins | a newer OPTIMIZE must not count |
  | No history at all | BLOCKED |
  | History read fails | BLOCKED — fail closed |
  | Foreign, non-Delta dependency | BLOCKED |
  | Dependency cycle | BLOCKED (not READY) |
  | Root view not found | BLOCKED |
  | Streaming table | checked from history; a stale one **blocks**; an unreadable one **blocks** |
  | Materialized view | read from the event log |
  | MV event log unreadable | BLOCKED |
  | Function dependency | a note; does not block |
  | Window edge: exactly at N hours | fresh |
  | Window edge: one second past | stale |
  | `fresh_hours` override | honoured |
  | `Config.fresh_hours` default | honoured when not overridden |
  | `fresh_hours <= 0` | error |
  | Future-dated write | ignored |
  | Naive `now` | localised to the config zone |
  | Same view twice in a list | two results |
  | Shared dependency across views | fetched **once** |
  | Failed lookup shared across views | fetched once, not retried |
  | Empty list of views | error |
  | Golden report text | exact match |

- Add one drift test: the SQL/set that decides "qualifying" must agree with the
  Python classifier for the whole operation zoo.

- The reference implementation has **176 tests**, all offline.

---

## 13. Packaging and release

- Package name `view-scheduler`, import name `view_scheduler`, src layout.
- One console script: `view-scheduler` (for the advisory tool).
- The wheel must contain **only** `view_scheduler/`. Not `rig/`, not `tests/`,
  not `tools/`, not the notebooks.
- Ship the wheel through the Unity Catalog Volume, so UC grants govern access.
- **Bump the version when behaviour changes.** The pre-gate wheel and the
  gate wheel were both `0.1.0` once, which made pinning impossible. The gate
  is `0.2.0`.
- Build, then install into a clean virtual environment and import the public
  names, before you upload.

---

## 14. Pitfalls found in review — do not reintroduce them

These were each found by an independent review and fixed. They will come back
if you are careless.

1. **A cycle reported READY.** Deduplicating on a single "seen" set left
   nothing to check, and `all(())` is `True`. Track *in-progress* and
   *resolved* names separately. A cycle is a blocker.
2. **Unquoted names in SQL and in the URL.** A hyphenated name becomes
   subtraction. An unquoted name is an injection surface. Quote per part;
   encode the path.
3. **Only the first result page was read.** Follow the chunks. Refuse a
   truncated result.
4. **A future-dated commit passed as fresh.** Ignore writes after the
   reference time.
5. **Malformed metadata crashed the walk.** Record it as a blocker instead.
6. **A whole type, parameter, or field that nothing uses.** The `quiet` flag
   and the print-by-default are the one deliberate exception; the owner chose
   that.
7. **A `_status` helper with an optional-argument signature that needs an
   `assert` to stay valid.** The assert vanishes under `python -O`. Use
   required keyword arguments.
8. **A hand-written sample output in the README.** It was wrong in three ways
   and nobody noticed. Generate it from the code.
9. **Docs that said the gate was never built**, while pointing the reader at
   them. Keep the docs true to the code.
10. **A README that said an unreadable MV "lands in skipped".** In the gate it
    **blocks**. Do not borrow the advisory tool's vocabulary for the gate.

---

## 15. Explicitly out of scope

- No writes of any kind.
- No CLI subcommand for the gate.
- No per-view window.
- No automatic refresh.
- No concurrency or 429 backoff in the walker (still serial).
- No per-table retention windows.
- No concurrency in the comparison tooling.

---

## 16. Open questions to resolve with the owner

Ask before assuming:

1. Is the advisory tool (`analyze`) still wanted, or is the gate the product?
2. Should the rig window extend past 2026-10-31?
3. Does the gate ever join the CLI, later?
4. Should the SQL read be bounded (a `LIMIT` or a pushed-down predicate), or is
   a full history read acceptable? The reference kept a single Python
   classifier as the source of truth to avoid drift.

---

## 17. Acceptance criteria — you are done when

1. `can_refresh("catalog.schema.view")` works with no other arguments, when
   `WAREHOUSE_ID` is set.
2. Every row of the decision table in section 3 is implemented and tested.
3. The report matches section 5, byte for byte, in a golden test.
4. All tests pass offline with no network.
5. The wheel installs into a clean environment and imports the public names.
6. The wheel was run against the live `workspace.sched_rig` schema, and:
   - `v_healthy`, `v_midnight`, `v_deep` return READY,
   - `v_realistic` returns BLOCKED, naming `t_weekly_mon`,
   - `v_broken` returns BLOCKED, naming `t_dormant`,
   - `st_stream` returns READY, through its history,
   - `mv_daily` returns READY through its event log.
7. No work email address appears anywhere in the repository or on GitHub.

---

## 18. Reference implementation

The current code is in the repository on branch `feat/gate-hardening`
(PR #5). Read it, but treat section 3 and section 7 as the source of truth.

Key files:

| File | Role |
|---|---|
| `src/view_scheduler/gate.py` | `can_refresh`, the result types, the report |
| `src/view_scheduler/walker.py` | dependency walk, cycle guard, shape validation |
| `src/view_scheduler/client.py` | the read-only I/O seam and the SQL |
| `src/view_scheduler/history.py` | the commit classifier |
| `src/view_scheduler/config.py` | the four settings |
| `src/view_scheduler/advice.py` | `advise`, the `Advice` result, the plain report |
| `src/view_scheduler/report.py` | the report text and warnings |
| `src/view_scheduler/run.py` | the low-level collector, and `analyze` |
| `tests/test_gate.py` | the gate test suite |
| `tests/conftest.py` | the fake Workspace |
| `rig/` | the fixture rig |
| `GENIE-PROMPT.md` | the original build prompt |
| `docs/01-plan.html` | the plan, including the gate |
