# View Materialization Scheduler

Two questions, two tools.

- **When** should this view run? → `advise`. It reads history and suggests a time.
- **Can** it run right now? → `can_refresh`, the gate. It checks today's state.

```
$ view-scheduler sales_gold.customer_360

VIEW   sales_gold.customer_360
       8 dependencies · last 30 days · times Asia/Kolkata

       SCHEDULE AT  06:25    safe — every job had finished, on all 30 days
       EARLIER      04:25    one job had not finished yet, on 1 of 30 days
```

## Read this before you rely on it

**The recommendation is advisory.** It says when a view *usually* can run; it
does not check today. If a dependency runs later than it ever has, the job
starts anyway and publishes stale data, silently. Pair it with the gate —
`can_refresh` — which checks every dependency's actual last write before the
refresh runs. Set the schedule from history; guard the run with the gate.

**The arithmetic is still being validated.** A 30-day fixture rig
(`rig/`) is running now to check the day-spread logic against real multi-day
commit history. Until that completes, treat output as indicative. Hence `0.6.0`.

## Install

### Databricks (recommended)

Ship the wheel through a Unity Catalog Volume, so access is governed by UC
grants rather than by GitHub accounts.

Once, to create the shared location:

```sql
CREATE VOLUME IF NOT EXISTS <catalog>.<schema>.libs;
GRANT READ VOLUME ON VOLUME <catalog>.<schema>.libs TO `<group>`;
```

Build and upload a release:

```bash
python -m build --wheel
databricks fs cp dist/view_scheduler-0.6.0-py3-none-any.whl \
  dbfs:/Volumes/<catalog>/<schema>/libs/
```

Keep old versions in the volume so running jobs can stay pinned.

Then, in a notebook:

```python
%pip install /Volumes/<catalog>/<schema>/libs/view_scheduler-0.6.0-py3-none-any.whl
dbutils.library.restartPython()
```

Or as a job library, including from a DAB:

```yaml
tasks:
  - task_key: recommend
    libraries:
      - whl: /Volumes/<catalog>/<schema>/libs/view_scheduler-0.6.0-py3-none-any.whl
```

### From source

For working on the package itself:

```bash
pip install -e ".[dev]"                                     # a checkout
pip install git+https://github.com/kiranb-1508/view-scheduler-docs.git
```

## Use

As a command:

```bash
export WAREHOUSE_ID=<sql warehouse id>
view-scheduler catalog.schema.my_view
view-scheduler catalog.schema.my_view --days 60 --buffer 30 --quiet
```

As a library:

```python
from view_scheduler import advise

advice = advise("sales.gold.customer_360")   # WAREHOUSE_ID from the env
advice.safe_time              # datetime.time(6, 25)  or None
advice.safe_without_outliers  # the SCHEDULE AT time, or None
advice.usual_time             # the typical day's finish, plus the buffer
advice.earlier_time           # datetime.time(4, 25)  or None
advice.outlier_days           # the dates that force the WITH OUTLIERS line
advice.next_day               # True when the (+1 day) marker applies
advice.dependencies           # one row per upstream job
advice.to_dict()              # JSON, for a job log
print(advice)                 # the report
```

`advise` prints the report and returns an `Advice`. Nothing is dropped
silently — anything unreadable comes back in `advice.unreadable` with a reason.

`analyze` is the low-level call. It returns a tuple
`(text, histories, skipped)` for code that needs the raw per-table history.

Four settings: `days_history` (30), `buffer_min` (15), `timezone`
(`Asia/Kolkata`), and `fresh_hours` (24) — the gate's window. There is a
standing rule to ask before adding a fifth.

### The report: three times, not two

A table with outlier days gets three lines. An outlier day is a day when a
rerun pushed the last job much later than usual.

```
       SCHEDULE AT  04:35    safe on 13 of 15 days — excludes 2 outlier days
       WITH OUTLIERS  06:25    safe on all 15 days
       EARLIER      04:15    one job had not finished yet, on 4 of 15 days
```

- `SCHEDULE AT` — the latest finish of a normal day, plus the buffer. It
  leaves out the outlier days. Schedule this time.
- `WITH OUTLIERS` — the latest finish of any day, plus the buffer. The report
  prints this line only when outlier days exist. This time would have worked on
  every day observed.
- `EARLIER` — the typical day's finish, plus the buffer. It is the median of the
  normal days, so the outlier days and the occasional slow load do not move it.
  It leaves out the days that missed a dependency, because no schedule time
  fixes a table that skipped a day. `advice.earlier_time` carries it, and it
  always equals `advice.usual_time`.

When there are no outlier days the report keeps the two-line shape:
`SCHEDULE AT` and `EARLIER` only. `advice.safe_without_outliers` carries the
`SCHEDULE AT` time, and `advice.usual_time` carries the typical day's finish.

### Tables that refresh more than once a day

The advisor counts the runs on a typical day. A table that refreshes two or
more times on a typical day is flagged `multi_daily`. The report gives the
usual time of each run, and the safe time of each run:

```
!  catalog.schema.orders refreshes 3 times a day (usually 02:00, 09:00, 17:00; safe at 02:15, 09:15, 17:15) — the schedule above uses its first run.
```

A table with a one-off rerun on one or two days is still an ordinary daily
table. Only a typical day counts. The gate reports the same flag on each
dependency.

### Compute mode: no warehouse needed

`advise` picks its history engine in this order:

1. an explicit `Workspace(warehouse_id="...")`
2. the `WAREHOUSE_ID` environment variable
3. the `SparkSession` the code already runs inside

Inside a Databricks notebook or job, option 3 applies. No warehouse id and no
access token are needed there. On a laptop only options 1 and 2 exist.

### Gate: is my view ready to refresh?

Before you materialize a view, ask whether its inputs are actually fresh. The
gate walks every dependency and checks that its last real write is inside a
rolling window. A daily table gets 24 hours by default. A table that runs less
often gets a wider window, so a Monday morning does not always block a
Monday-to-Friday table.

```python
from view_scheduler import can_refresh

result = can_refresh("sales.gold.v_orders")          # WAREHOUSE_ID from $env
if result.ready:
    ...run the refresh...
else:
    print(result)                                    # says exactly what to fix
```

A list works the same way. You get one verdict per view, and one overall:

```python
result = can_refresh(["sales.gold.v_orders", "sales.gold.v_revenue"])
print(result.ready)                 # True only when every view is ready
print(result.blockers())            # ('sales.silver.daily_sales',)
print(result.to_dict())             # JSON, for a job log
```

```
GATE  BLOCKED   2 views, window 24h, times Asia/Kolkata

  READY   sales.gold.v_orders
      fresh  sales.silver.daily_orders  (MANAGED)  last refresh 2026-10-07 04:00 IST (11h 48m ago)

  BLOCKED sales.gold.v_revenue
      fresh  sales.silver.orders        (MANAGED)  last refresh 2026-10-07 04:00 IST (11h 48m ago)
      stale  sales.silver.daily_sales   (MANAGED)  last refresh 2026-10-05 04:00 IST (2d 11h ago) — older than the 24h window

To unblock, refresh:
  sales.silver.daily_sales   (needed by sales.gold.v_revenue)
```

The gate decides; it never refreshes. It is read-only, like `analyze`. The rules
are the whole table:

| Case | Verdict |
|---|---|
| Wrote inside the window | fresh |
| Wrote before the window | stale — **blocks** |
| No qualifying write in its history | **blocks** |
| Cannot be read (missing, no permission) | **blocks** — the gate fails closed |
| A weekly-ish table on a trusted schedule | checked against its own wider window — see below |
| A table seen on all seven weekdays | a daily table with outages — keeps the daily window |
| A table with a thin or new history | checked on the daily window, whatever it looks like |
| Streaming table | checked from its history, like any table — one on a schedule is stale between runs |
| Materialized view | read from its pipeline event log |
| Function dependency (a UDF) | a note, not a blocker |

#### Each table is checked against its own schedule

A daily table gets the `fresh_hours` window. A table that runs less often gets
a wider window.

The gate reads the weekdays the table actually wrote on. The window is the
longest gap between those run weekdays, plus one, times `fresh_hours`:

| Pattern | Window at `fresh_hours=24` |
|---|---|
| Every day | 24h |
| Monday to Saturday | 72h |
| Monday to Friday | 96h |
| Mondays only | 192h |

The gate uses this wider window only for a trusted table. A trusted table does
not run on all seven weekdays, and every weekday it wrote on appears at least
twice. A table seen on all seven weekdays is a daily table with outages, so it
keeps the daily window. A thin or new table keeps the daily window too. The
gate fails closed.

A missed run is still caught within one day. A Mon-Fri table that skips Friday
is 98 hours old on Monday. That is past its 96-hour window, so the gate blocks.

`Workspace(warehouse_id=...)` and `Config.fresh_hours` are optional. Without a
`Workspace`, the gate picks its engine in the same order as `advise` — see
[Compute mode](#compute-mode-no-warehouse-needed). Inside a Databricks notebook
or job the Spark session applies, so `can_refresh("catalog.schema.view")` needs
no setup at all.

#### Options that relax the daily check

`can_refresh` takes three keyword options. Each one changes how the gate
decides.

```python
from view_scheduler import can_refresh

result = can_refresh(
    "catalog.schema.my_view",
    ignore_tables=["catalog.schema.audit_log"],   # pass, whatever its history shows
    ignore_non_daily=True,                        # pass a table with no write in history
    check_multi_daily=True,                       # shorten the window for a busy table
)
```

- `ignore_tables=["catalog.schema.table", ...]` — the gate passes those tables
  whatever their history shows. The report says `ignored by caller`.
- `ignore_non_daily=True` — the gate passes a table with no write in its
  history as a note. The cadence check above is the default now, so this option
  covers only that one case. The report says `static table (no refresh in
  history) — ignored`.
- `check_multi_daily=True` — the gate checks a table that runs many times a day
  against a shorter window. The window becomes the earned window divided by the
  runs per day. Three runs a day for a Mon-Fri table with `fresh_hours=24`
  gives a 32-hour window: 96 divided by 3.

#### Mark a table in Unity Catalog

A table can mark itself. Set the Unity Catalog table property
`view_scheduler.mode`:

```sql
ALTER TABLE catalog.schema.table SET TBLPROPERTIES ('view_scheduler.mode' = 'event_driven');
```

Use one of these values: `event_driven`, `event-driven`, `on_demand`,
`on-demand`, `static`, `weekly`, or `ignore`.

- `event_driven`, `on_demand`, `static`, and `ignore` — the gate does not block
  on this table. The report says `<mode> table — ignored`.
- `weekly` — the gate forces the widest gap between run weekdays: seven days.
  The window is `(7 + 1) * fresh_hours`, or 192 hours at the default.

**Permissions.** The caller needs `SELECT` on each dependency (for
`DESCRIBE HISTORY`) and `USE` on the catalogs and schemas involved. Materialized
views are read through their pipeline event log, which by default only the
pipeline's run-as user can read. Without that access the gate **blocks** the MV:
it reports `history unreadable (permission)`. The call itself does not raise.

## Design docs

| File | What it is |
|---|---|
| [docs/01-plan.html](docs/01-plan.html) | The plan. Start here — output shape, what's built, what was deliberately cut |
| [docs/02-scheduler-design.html](docs/02-scheduler-design.html) | Scheduler design: architecture, code flow, four steps, worked example |
| [docs/03-impact-design.html](docs/03-impact-design.html) | Impact analysis: given a table, what depends on it. Separate tool, separate data source |
| [docs/04-library-flow.html](docs/04-library-flow.html) | Visual end-to-end flow: input, functions, branches, recommendation, and report |
| [docs/05-interactive-guide.html](docs/05-interactive-guide.html) | Interactive guide: clickable flow, a gate simulator, the design decisions, and the open findings |
| [docs/06-code-walkthrough.html](docs/06-code-walkthrough.html) | Code walkthrough: the module map, call traces, a class tour, and a searchable index of all 66 functions |
| [rig/README.md](rig/README.md) | The 30-day fixture rig and its checkpoints |
| [GENIE-PROMPT.md](GENIE-PROMPT.md) | Build prompt for Databricks Genie Code, phased with verification gates |

Section 08 of the scheduler design lists what the docs describe but nobody
built. The runtime gate is no longer on that list — it ships as `can_refresh`.

## The two things most likely to bite

1. **Operation class comes from `operationParameters.mode`, not the operation
   name.** The commonest Delta operation name is plain `WRITE`; only its mode
   says whether it replaced or appended. Classify by name and most tables read
   as dormant — a confident wrong answer, not an error.
2. **`DESCRIBE HISTORY` carries no partition values.** Verified against the docs.
   Any design that attributes commits to business dates via partitions is built
   on an input that does not exist.

## Development

```bash
pip install -e ".[dev]"
pytest
```

221 tests, all offline — no Spark session, no network, no credentials.
`tools/` holds diagnostic scripts that run against a live workspace;
`rig/` builds and checks the 30-day fixture. Neither ships in the wheel.
