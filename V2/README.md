# View Materialization Scheduler V2

V2 is the implementation scaffold for the simpler, read-only scheduler described
in the design documents.

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

## Implementation order

1. Add fixtures and tests for history classification.
2. Implement the dependency walker with memoization, cycle protection, and a
   depth limit.
3. Implement per-table finish times and the stale-ready rollup.
4. Render the recommendation and applicable warnings.
5. Validate against representative ADF, Databricks, and Synapse-fed tables.

See the [library flow diagram](../docs/04-library-flow.html) before implementing
the modules.
