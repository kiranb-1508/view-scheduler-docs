# View Materialization Scheduler

Two questions, two tools.

- **When** should this view run? → `advise`. It reads history and suggests a time.
- **Can** it run right now? → `can_refresh`, the gate. It checks today's state.

```
$ view-scheduler sales_gold.customer_360

VIEW   sales_gold.customer_360
       8 dependencies · last 30 days · times Asia/Kolkata

       SCHEDULE AT  06:25    safe — every job had finished, on all 30 days
       EARLIER      05:45    one job had not finished yet, on 1 of 30 days
```

## Read this before you rely on it

**The recommendation is advisory.** It says when a view *usually* can run; it
does not check today. If a dependency runs later than it ever has, the job
starts anyway and publishes stale data, silently. Pair it with the gate —
`can_refresh` — which checks every dependency's actual last write before the
refresh runs. Set the schedule from history; guard the run with the gate.

**The arithmetic is still being validated.** A 30-day fixture rig
(`rig/`) is running now to check the day-spread logic against real multi-day
commit history. Until that completes, treat output as indicative. Hence `0.4.0`.

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
databricks fs cp dist/view_scheduler-0.4.0-py3-none-any.whl \
  dbfs:/Volumes/<catalog>/<schema>/libs/
```

Keep old versions in the volume so running jobs can stay pinned.

Then, in a notebook:

```python
%pip install /Volumes/<catalog>/<schema>/libs/view_scheduler-0.4.0-py3-none-any.whl
dbutils.library.restartPython()
```

Or as a job library, including from a DAB:

```yaml
tasks:
  - task_key: recommend
    libraries:
      - whl: /Volumes/<catalog>/<schema>/libs/view_scheduler-0.4.0-py3-none-any.whl
```

### From source

For working on the package itself. Requires access to this private repo.

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
advice.safe_time      # datetime.time(6, 25)  or None
advice.earlier_time   # datetime.time(5, 45)  or None
advice.next_day       # True when the (+1 day) marker applies
advice.dependencies   # one row per upstream job
advice.to_dict()      # JSON, for a job log
print(advice)         # the report
```

`advise` prints the report and returns an `Advice`. Nothing is dropped
silently — anything unreadable comes back in `advice.unreadable` with a reason.

`analyze` is the low-level call. It returns a tuple
`(text, histories, skipped)` for code that needs the raw per-table history.

Four settings: `days_history` (30), `buffer_min` (15), `timezone`
(`Asia/Kolkata`), and `fresh_hours` (24) — the gate's window. There is a
standing rule to ask before adding a fifth.

### Gate: is my view ready to refresh?

Before you materialize a view, ask whether its inputs are actually fresh. The
gate walks every dependency and checks that its last real write is inside a
rolling window (24 hours by default).

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
| Streaming table | checked from its history, like any table — one on a schedule is stale between runs |
| Materialized view | read from its pipeline event log |
| Function dependency (a UDF) | a note, not a blocker |

`Workspace(warehouse_id=...)` and `Config.fresh_hours` are optional. Without a
`Workspace`, the gate reads `WAREHOUSE_ID` from the environment, so the
one-liner above is enough inside a job.

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

176 tests, all offline — no Spark session, no network, no credentials.
`tools/` holds diagnostic scripts that run against a live workspace;
`rig/` builds and checks the 30-day fixture. Neither ships in the wheel.
