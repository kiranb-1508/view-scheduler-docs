# View Materialization Scheduler

Answers: *given a Unity Catalog view, what is the earliest time each day its
materialization can be scheduled so every dependency has refreshed?*

```
$ view-scheduler sales_gold.customer_360

VIEW   sales_gold.customer_360
       8 dependencies · 30 days history · times Asia/Kolkata

       START AT 06:25   safe — later than all 30 days
       or       05:45   tighter — would have been early on 1 day in 30
```

## Read this before you rely on it

**The recommendation is advisory.** Nothing verifies at runtime that
dependencies actually refreshed. If a dependency runs later than it ever has,
the job starts anyway and publishes stale data, silently. The runtime gate
described in `docs/01-plan.html` is a *proposal* — it was never built.

**The arithmetic is still being validated.** A 30-day fixture rig
(`rig/`) is running now to check the day-spread logic against real multi-day
commit history. Until that completes, treat output as indicative. Hence `0.1.0`.

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
databricks fs cp dist/view_scheduler-0.1.0-py3-none-any.whl \
  dbfs:/Volumes/<catalog>/<schema>/libs/
```

Keep old versions in the volume so running jobs can stay pinned.

Then, in a notebook:

```python
%pip install /Volumes/<catalog>/<schema>/libs/view_scheduler-0.1.0-py3-none-any.whl
dbutils.library.restartPython()
```

Or as a job library, including from a DAB:

```yaml
tasks:
  - task_key: recommend
    libraries:
      - whl: /Volumes/<catalog>/<schema>/libs/view_scheduler-0.1.0-py3-none-any.whl
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
from view_scheduler import analyze, Config, Workspace

ws = Workspace(warehouse_id="...")
text, histories, skipped = analyze("catalog.schema.my_view", ws, Config())
print(text)
```

`analyze` returns the rendered report, the per-table history objects, and
anything that could not be read. Nothing is ever dropped silently — an
unreadable dependency comes back in `skipped` with a reason.

Three settings, and there is a standing rule to ask before adding a fourth:
`days_history` (30), `buffer_min` (15), `timezone` (`Asia/Kolkata`).

**Permissions.** The caller needs `SELECT` on each dependency (for
`DESCRIBE HISTORY`) and `USE` on the catalogs and schemas involved. Materialized
views are read through their pipeline event log, which by default only the
pipeline's run-as user can read; without that access the MV lands in `skipped`
rather than failing the run.

## Design docs

| File | What it is |
|---|---|
| [docs/01-plan.html](docs/01-plan.html) | The plan. Start here — output shape, what's built, what was deliberately cut |
| [docs/02-scheduler-design.html](docs/02-scheduler-design.html) | Scheduler design: architecture, code flow, four steps, worked example |
| [docs/03-impact-design.html](docs/03-impact-design.html) | Impact analysis: given a table, what depends on it. Separate tool, separate data source |
| [rig/README.md](rig/README.md) | The 30-day fixture rig and its checkpoints |
| [GENIE-PROMPT.md](GENIE-PROMPT.md) | Build prompt for Databricks Genie Code, phased with verification gates |

Section 08 of the scheduler design lists what the docs describe but nobody
built — read it before assuming a feature exists.

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

135 tests, all offline — no Spark session, no network, no credentials.
`tools/` holds diagnostic scripts that run against a live workspace;
`rig/` builds and checks the 30-day fixture. Neither ships in the wheel.
