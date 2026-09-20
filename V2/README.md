# View Materialization Scheduler V2

V2 is the first implementation of the simpler, read-only scheduler described in
the design documents.

## Goal

Given a Unity Catalog view, report when its materialization can be scheduled based
on dependency history. The library does not wait for dependencies or modify user
tables.

## Planned shape

- One small Python module with pure computation functions.
- One thin runner for the Unity Catalog API and `DESCRIBE HISTORY` calls.
- Injected metadata/history readers so the core can be tested without Spark or a
  network connection.
- Explicit skipped dependencies and warning flags; nothing is silently dropped.
- Clock-time calculations in the configured timezone.
- `safe`, `sooner`, and per-weekday recommendations with the evidence behind
  each result.

## Current implementation

- History classification uses `operationParameters.mode` and preserves degraded
  or unclassified writes as visible flags.
- Dependency walking uses memoization, cycle protection, a depth limit, and a
  named skipped list.
- Per-table finish times use the configured timezone and clock minutes.
- Recommendation uses stale-ready semantics, safe/sooner times, and weekday
  results without a runtime gate.
- The runner is the only place that imports Spark or the Databricks SDK.
- Local tests cover the core without Spark or a network connection.

See the [library flow diagram](../docs/04-library-flow.html) before implementing
the modules.

## Files

- `lazy_scheduler.py` - library, pure computation, injected I/O seams.
- `runner.py` - thin Databricks SDK and Spark adapter.
- `test_lazy_scheduler.py` - local pytest coverage for the core logic.
