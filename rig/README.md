# 30-day fixture rig

Produces real, multi-day Delta commit history so the scheduler's day-spread
arithmetic can be validated against something other than fixtures.

Delta stamps a commit when it happens and there is no supported override, so
this cannot be compressed: 30 days of history takes 30 days. What it *can* do is
be predictable — every write is a function of `(table, date)` through md5, so the
correct report is knowable in advance and the analyzer's output can be asserted
rather than eyeballed.

## Window

`2026-09-01` → `2026-09-30`. Past the end date the task pauses all seven jobs
instead of writing.

## Layout

| File | What it is |
|---|---|
| `plan.py` | The write matrix. Pure — no I/O, no wall clock. The specification both the writer and the oracle read. |
| `setup_rig.py` | One-shot DDL. Creates the schema, 13 tables, 7 views, an MV and a streaming table. |
| `write_slot.py` | The job task. Asks `plan.py` what its slot writes today. |
| `oracle.py` | What the analyzer *should* say, derived from the plan. |
| `check.py` | Runs the analyzer against real data and diffs it against the oracle. |
| `databricks.yml` | The seven jobs. |

## Jobs

Seven, one per slot, all `Asia/Kolkata`:

```
00:20   02:00   04:00   06:10   09:00   17:00   23:40
```

Lateness comes from *which slot fires*, decided by the date hash — not from a
task sleeping. Sleeping to fake jitter would burn hours of compute for nothing.

## What each table is for

| Table | Covers |
|---|---|
| `t_daily_tight` | baseline; `usual` ≈ `worst` |
| `t_daily_late_tail` | two late days, so `safe` and `tighter` diverge |
| `t_daily_append` | incremental qualifying path |
| `t_append_zero` | zero-row append still counts (the pipeline ran) |
| `t_overwrite_zero` | zero-row overwrite counts, via a different class |
| `t_merge` | row metrics under `numTargetRows*` keys |
| `t_weekly_mon` | weekday labelling; `missed` stays 0 |
| `t_flaky` | three skipped days become `missed` |
| `t_maintenance_only` | a metadata-only commit is not a refresh |
| `t_multi_daily` | three writes a day; the first is the finish time |
| `t_near_midnight` | 00:20 IST is the *previous* UTC day |
| `t_late_evening` | the other side of that boundary |
| `t_dormant` | the "never refreshed" warning |
| `mv_daily` | the materialized-view event-log path |
| `st_stream` | `STREAMING_TABLE` gating |

## Running it

```bash
export WAREHOUSE_ID=<your warehouse>

python rig/setup_rig.py              # once
cd rig && databricks bundle deploy   # once

python rig/check.py                  # at any checkpoint
python rig/check.py v_realistic      # one view
```

`check.py` exits non-zero on any disagreement with the plan.

## Checkpoints

| Day | What to look for |
|---|---|
| 1 | Every slot fired; each table has its day-1 commits |
| 3 | Day attribution correct on both midnight tables — where a timezone bug shows |
| 8 | `t_weekly_mon` labelled `Mons`; `t_flaky` and `t_daily_late_tail` have each had one bad day |
| 30 | Full run of every view against the oracle |

The late days and the flaky skips are deliberately spread across the window.
Ranked purely by hash all five landed in the final week, which would have left
the early checkpoints blind.

## Safety

Everything is confined to `workspace.sched_rig`. Both `setup_rig.py` and
`write_slot.py` refuse any statement that does not reference it. The scheduler
package itself (`src/scheduler/`) is strictly read-only and is not involved in
writing any of this.

`delta.logRetentionDuration` is set to 60 days on the rig's own tables. At the
30-day default, day-1 commits age out of `DESCRIBE HISTORY` exactly as the
experiment completes. No table outside the rig is altered.

## Stopping it

The rig stops itself after the window closes. To stop it sooner:

```bash
cd rig && databricks bundle destroy       # removes the seven jobs
```

Then drop the data when you no longer want it:

```sql
DROP SCHEMA workspace.sched_rig CASCADE;
```

The MV and streaming table carry their own schedules and are removed with the
schema.
