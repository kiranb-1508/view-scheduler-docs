# View Materialization Scheduler — design docs

Answers: *given a Unity Catalog view, what is the earliest time each day its
materialization can be scheduled so every dependency has refreshed?*

Open the HTML files in a browser.

| File | What it is |
|---|---|
| [docs/01-plan.html](docs/01-plan.html) | The plan. Start here — output shape, what's built, what was deliberately cut |
| [docs/02-scheduler-design.html](docs/02-scheduler-design.html) | Scheduler design: architecture, code flow, four steps, worked example |
| [docs/03-impact-design.html](docs/03-impact-design.html) | Impact analysis: given a table, what depends on it. Separate tool, separate data source |
| [GENIE-PROMPT.md](GENIE-PROMPT.md) | Build prompt for Databricks Genie Code, phased with verification gates |

## The two things most likely to bite

1. **Operation class comes from `operationParameters.mode`, not the operation
   name.** The commonest Delta operation name is plain `WRITE`; only its mode
   says whether it replaced or appended. Classify by name and most tables read
   as dormant — a confident wrong answer, not an error.
2. **`DESCRIBE HISTORY` carries no partition values.** Verified against the docs.
   Any design that attributes commits to business dates via partitions is built
   on an input that does not exist.
