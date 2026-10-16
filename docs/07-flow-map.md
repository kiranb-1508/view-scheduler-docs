# Flow map — what runs, and where to change it

One page. The public surface, the shared spine, the two call chains, and the
file to open for each likely change.

## 1. The public surface

Three names, exported by `__init__.py`.

| Name | Module | Question it answers |
|---|---|---|
| `advise(view)` | `advice.py` | When can this view run each day? Advisory. |
| `can_refresh(view)` | `gate.py` | Is every dependency fresh right now? The gate. |
| `analyze(view)` | `run.py` | Low-level. Report text, plus raw history. |

`can_refresh` is the product. `advise` and `analyze` share one spine with it.

## 2. The shared spine

Both tools take the same four steps. Only the last step differs.

```
  1 WALK            2 READ              3 CLASSIFY        4 DECIDE
  --------          --------            ----------        --------
  walker.py    →    client.py      →    history.py   →    gate.py       (gate)
                    (only I/O)          (pure)            recommend.py  (advise)
```

1. **Walk** — follow the view's dependencies down to the physical tables.
2. **Read** — fetch each table's history. One file talks to Databricks: `client.py`.
3. **Classify** — decide whether a commit is a real refresh. `history.py`, pure.
4. **Decide** — the gate measures freshness. The advisor computes a time.

The report is written last: `gate.py` for the gate, `report.py` for the advisor.

## 3. The two call chains

**`advise`**

```
advice.advise
  └─ run.collect
       ├─ walker.walk            → client.Workspace.get_table   (walk)
       ├─ client.read_commits    → history.qualifying          (read + classify)
       ├─ history.summarize      → per-table finish times
       └─ recommend.recommend    → safe / earlier times        (decide)
  └─ report.render              → the text                    (report)
```

**`can_refresh`**

```
gate.can_refresh
  ├─ walker.walk                → client.Workspace.get_table   (walk)
  ├─ client.read_latest_commit  → client.read_commits          (read)
  │                             → history.qualifying           (classify)
  ├─ gate._history_summary       → the latest write, cadence, window (decide)
  ├─ gate._verdict_for           → fresh or stale               (decide)
  └─ GateResult.render          → the text                     (report)
```

Both caches live in `can_refresh`: one table is read once per call, and a
failure is remembered, not retried.

## 4. Module roles, one line each

| File | Role |
|---|---|
| `__init__.py` | The public names. Nothing else. |
| `advice.py` | `advise`, `Advice`. The advisory front door. |
| `gate.py` | `can_refresh`, the result types, the gate report. |
| `run.py` | `collect` (shared) and `analyze`. |
| `walker.py` | The dependency tree, the cycle guard, the depth cap. |
| `client.py` | The only I/O. Metadata, SQL, history, MV event log. |
| `history.py` | The commit classifier and the per-day rollups. Pure. |
| `recommend.py` | Finish times in, two clock times out. Pure. |
| `report.py` | The advice report text and its warnings. |
| `models.py` | Shared data shapes. No logic. |
| `config.py` | Four settings. |
| `cli.py` | The `view-scheduler` command. |
| `obs.py` | One log line per external call. |

## 5. Where do I change X?

| I want to change… | Open this | Touch this |
|---|---|---|
| The freshness window default | `config.py` | `Config.fresh_hours` |
| What counts as a refresh | `history.py` | `FULL_REPLACE`, `INCREMENTAL`, `IGNORE`, `classify` |
| The gate report wording | `gate.py` | `GateResult.render`, `_dep_line` |
| The advice report wording | `report.py` | `render`, `warnings` |
| How safe / earlier is computed | `recommend.py` | `recommend`, `_add_buffer` |
| Which object types gate a view | `walker.py` | `GATING`, the `kind in GATING` branch |
| The SQL that reads history | `client.py` | `read_delta_history`, `read_mv_refreshes` |
| A new error category | `client.py` | `failure_category` |
| The recursion limit | `walker.py` | `MAX_DEPTH` (60) |
| The timezone default | `config.py` | `Config.timezone` |
| The JSON a job logs | `gate.py` / `advice.py` | `to_dict` |
| A new public function | `__init__.py` | `__all__`, plus its module |

## 6. The honest size

- Total: about 1,500 lines across 13 files.
- The part that decides a verdict: `history.py` + `recommend.py` + `gate.py`,
  about 600 lines.
- The part that talks to Databricks: one file, `client.py`.
- The walk: one file, `walker.py`, about 140 lines.

Two modules carry the real weight: `walker.py` (the tree) and `client.py` (the
I/O). Understand those two and the rest is arithmetic and text.

The two functions are not independent. They share the walk and the classifier.
That shared spine is what a single notebook cell would have to duplicate, and
the seam it would lose is the one the offline tests inject a fake `Workspace`
into.
