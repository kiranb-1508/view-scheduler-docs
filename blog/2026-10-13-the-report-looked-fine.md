# The Report Looked Fine. It Wasn't.

*A short story about a silent data bug, and the small tool I built to kill it.*

## The request

"Move the report refresh to 8 AM. Leadership wants the numbers earlier."

Sure. One job schedule change. Five minutes of work.

Except the report reads from a materialized view. That materialized view refreshes from other views and base tables. Those tables finish when they finish — some from nightly jobs, some from streams, one from a warehouse on another network. Nobody wrote the finish times down anywhere.

So I dug. Lineage one level down, then two. Open each table's history, work out when it last loaded, guess when it usually finishes. An hour later I had an answer — half of it a guess. I moved the schedule and hoped.

It was the fourth time that month I'd gotten a similar request — move it earlier, move it later, is 6 AM safe — and each one ended the same way: an hour of digging.

## The bug nobody sees

Here's the thing about moving a refresh earlier. If an upstream table isn't ready, the refresh doesn't fail. It doesn't error, doesn't alert, doesn't call anyone. It runs, and it happily refreshes the view with *yesterday's* data.

The report goes out on time. The date on it is today. The numbers in it are not.

That's the worst kind of bug. A pipeline failure screams. A silent stale refresh whispers, and the whispers sound like this: "These numbers don't match the finance deck." Three weeks later. By email.

A confident wrong answer is more expensive than an error.

## The tool, in one paragraph

So I wrote a small library for our Databricks views that answers two questions before anything runs. **When should this refresh?** It reads the last 30 days of real commit history across all the view's dependencies, learns when each one actually finishes, and suggests the earliest safe time — with a small buffer. **Can I refresh right now?** It walks the dependency chain and blocks the refresh if any table is stale, untouched, or unreadable. One rule carries it: if the tool can't check something, it blocks. Never approves. It takes maybe a minute, and it costs less than the hour of digging — or the wrong report.

## How it works

Under the hood, both functions take the same road — only the last step differs:

```
view ──▶ WALK ──▶ READ ──▶ CLASSIFY ──▶ DECIDE
         │        │         │            │
   follow the   pull each  decide if    gate: fresh or
   dependency   table's    a commit is   BLOCKED, or
   chain down   history    a real        advisor: a safe
   to tables    from UC    refresh       time with a buffer
```

```
advise(view)        → "the earliest safe hour is 09:15"
can_refresh(view)   → READY, or BLOCKED — and names the stale table
```

Four steps, no magic: walk the dependency tree down to real tables, read each one's commit history, classify which commits are actual refreshes, then decide — a verdict for the gate, a clock time for the advisor.

## What it looks like

One call per question. Here is the gate telling me, before a refresh job runs, that one upstream table is two days old:

```
>>> from view_scheduler import can_refresh
>>> print(can_refresh("sales.gold.v_revenue"))

GATE  BLOCKED   1 view, window 24h, times Asia/Kolkata

  BLOCKED sales.gold.v_revenue
      fresh  sales.silver.orders        (MANAGED)  last refresh 2026-10-13 04:00 (2h 5m ago)
      stale  sales.silver.daily_sales   (MANAGED)  last refresh 2026-10-11 04:00 (2d 2h ago) — older than the 24h window

To unblock, refresh:
  sales.silver.daily_sales   (needed by sales.gold.v_revenue)
```

It found the stale table, said which view needs it, and refused to approve anything. And this check isn't tied to reports: any job that reads these tables can make the same call its first step — run the gate, and if it says BLOCKED, stop right there, before dashboards refresh, alerts fire, or the next task in the pipeline runs on data that isn't there yet. The check is read-only and takes seconds, so it slots into any job as a cheap insurance step.

And when someone asks to move a schedule earlier, the advisor reads the same history and suggests a time with its reasoning printed next to it:

```
$ view-scheduler sales.gold.customer_360

VIEW   sales.gold.customer_360
       3 dependencies · last 30 days · times Asia/Kolkata

       SCHEDULE AT  06:15   safe — every job had finished, on all 30 days
       EARLIER      05:15   one job had not finished yet, on 2 of 30 days

DEPENDENCY             RUNS   USUAL   LATEST   DAYS   MISSED
silver.daily_orders    daily  03:00   03:00    30     0
silver.orders          daily  04:30   06:00    30     0
silver.daily_sales     daily  04:45   04:45    29     1

!  silver.daily_sales did not run on 1 of 30 days — no schedule time fixes that
```

`SCHEDULE AT` is the latest finish of a normal day, plus a 15-minute buffer. A late rerun gets its own `WITH OUTLIERS` line, so one bad day cannot drag the daily schedule. The `EARLIER` time says how the typical day looks without that padding — and how many days it would have been wrong. The evidence is printed, not hidden behind a score.

## Try it

The library is open source and on PyPI: [view-scheduler on PyPI](https://pypi.org/project/view-scheduler/) ([source on GitHub](https://github.com/kiranb-1508/view-scheduler-docs)). Install it, point it at a Unity Catalog view, and ask it when you should schedule.

```
pip install view-scheduler
```


