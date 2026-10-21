# Library Hardening (v0.7.0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the gate cadence-aware by default, make the advisor's headline honest about unreadable dependencies, use one outlier rule, document the first-commit tradeoff, and retry transient read failures.

**Architecture:** All changes stay inside the existing spine (walk → read → classify → decide). No new module, no new I/O path, no new dependency. `recommend.py` stays pure. `gate.py` decides freshness. `client.py` is still the only I/O seam, and gains the only new runtime behavior (retry).

**Tech Stack:** Python 3.10+, pytest, databricks-sdk (already a dependency). No new packages.

**Spec:** `HANDOFF.md` (the requirements document), plus the owner's decisions of 2026-10-16, recorded below. The plan argues from both.

## Owner decisions, 2026-10-16

1. No gate CLI. The library comes first. Out of scope.
2. Cadence-aware default freshness: wanted. This plan fixes it (Task 2).
3. No advise headline when a dependency is unreadable: wanted (Task 3).
4. Keep one outlier detector, not two (Task 1).
5. The first-commit tradeoff: document it. No new setting (Task 4). A rig probe is an open question, not a task.
6. Retries for transient read failures: wanted (Task 5).

## Global Constraints

- Read-only. The package never writes to user data. (HANDOFF 7.1)
- Fail closed. A false BLOCK beats a false READY. Where a change can lean either way, it leans closed. (HANDOFF 7.2)
- All tests run offline. No network, no Spark, no credentials, no real sleeping. (HANDOFF 12)
- Four settings. No new `Config` fields. (README, standing rule)
- ASD-STE 100 writing style in every user-visible string, comment, and doc. (HANDOFF 7.4)
- No new dependencies. The retry helper is hand-rolled.
- Docs stay true to code in the same commit. (HANDOFF 14.9)
- Golden text changes are deliberate. Update the golden test in the same commit. Say why in the commit body. (HANDOFF 12, 14.8)
- Version stays 0.6.0 until Task 6, then one bump to 0.7.0. (HANDOFF 13)

## Review Focus

The five inputs most likely to bite, and the task that pins each:

1. **A new or sparse table must not earn a relaxed window.** Fewer than 10 observed run days, or a weekday seen once, keeps the strict daily window. (Task 2, gate tests)
2. **A rerun day must not flip a daily table's label.** Extra commits on a few days keep the "daily" label, so the window stays 24h. (Task 2, label tests)
3. **A permanent MV rejection must not be retried.** `expect_table_not_view` fails once, then falls back to the event log. Three retries per dependency would triple the latency of every MV check. (Task 5)
4. **An unreadable dependency must null every time, in print and in JSON.** `to_dict` and the printed report must agree. A caller that logs JSON must not see a confident `safe_time`. (Task 3)
5. **A retry that re-reads history must not defeat the clock-skew guard.** A future-dated commit still reads as ignored after a retry. (Task 5; the guard itself is existing gate behavior and stays.)

---

### Task 1: One outlier rule

**Files:**
- Modify: `src/view_scheduler/recommend.py` (`_outlier_day`, `_outlier_dates`, `dependency_rows`)
- Test: `tests/test_recommend.py`, `tests/test_report.py`

**Interfaces:**
- Produces: `_outlier_dates(finish_times: dict) -> tuple[date, ...]` is the one rule. Mean absolute deviation, `OUTLIER_FLOOR_MIN = 30`, `OUTLIER_MAD_K = 3`. Unchanged signature.
- Produces: `DependencyRow.outlier_day` = the row's latest-clock day, when that day is in `_outlier_dates(row's own finish_times)`, else `None`.
- Removes: `_outlier_day` (the MAD-based rule). Nothing imports it outside this module.

**Why:** `_outlier_day` uses the median absolute deviation. `_outlier_dates` uses the mean. Two rules in one module can disagree about the same day. A row can warn "outlier" while the recommendation excludes a different set. Keep the mean-AD rule: it is the conservative one (it flags fewer days, so `SCHEDULE AT` lands later, so it fails closed).

- [ ] **Step 1: Write the failing test**

In `tests/test_recommend.py`:

```python
def test_borderline_late_days_are_not_outliers_anywhere():
    """4 of 12 days run 1h late. MAD would flag the latest day. The one
    mean-AD rule must not flag it, in the row or in the recommendation."""
    days = {date(2026, 8, d): datetime(2026, 8, d, 4, 0, tzinfo=UTC)
            for d in range(1, 13)}
    for d in (3, 5, 8, 11):
        days[date(2026, 8, d)] = datetime(2026, 8, d, 5, 0, tzinfo=UTC)
    rows = dependency_rows([TableHistory("c.s.x", finish_times=days)],
                           sorted(days))
    assert rows[0].outlier_day is None
    assert _outlier_dates(days) == ()
```

- [ ] **Step 2: Run it, confirm it fails**

Run: `python -m pytest tests/test_recommend.py::test_borderline_late_days_are_not_outliers_anywhere -v`
Expected: FAIL — `outlier_day` is the late day, because `_outlier_day` uses MAD.

- [ ] **Step 3: Implement the consolidation**

Delete `_outlier_day`. In `dependency_rows`, compute `outlier_day` from `_outlier_dates(history.finish_times)`: the day of the latest clock time, if that day is in the outlier set. Extend the `_outlier_dates` docstring: it is the one rule, used at both levels.

- [ ] **Step 4: Run the full suite; update the goldens that changed**

Run: `python -m pytest -q`
Expected: `test_report.py::test_outlier_day_is_named_when_worst_is_far_above_usual` may fail if its fixture day was a MAD-only outlier. If so, retarget the fixture so the day clears the mean-AD rule (a rarer, clearly separated late day). Do not weaken the assertion.

- [ ] **Step 5: Commit**

```bash
git add src/view_scheduler/recommend.py tests/test_recommend.py tests/test_report.py
git commit -m "One outlier rule: mean absolute deviation, at both levels"
```

---

### Task 2: Cadence-aware default freshness

**Files:**
- Modify: `src/view_scheduler/recommend.py` (`_weekday_label`, new `max_gap_days`)
- Modify: `src/view_scheduler/gate.py` (`_history_summary`, `_verdict_for`, `ReadSummary`)
- Test: `tests/test_recommend.py`, `tests/test_gate.py`
- Docs: `README.md` (gate section), `HANDOFF.md` (10, 11, 15)

**Interfaces:**
- Produces in `recommend.py`:

```python
def max_gap_days(seen: frozenset[int]) -> int:
    """Longest gap between run weekdays, cyclically. Mon–Fri -> 3,
    Mon–Sat -> 2, Mondays-only -> 7, daily -> 1."""
```

- Produces in `gate.py`:

```python
@dataclass(frozen=True)
class ReadSummary:
    latest: datetime | None
    cadence: str                    # "daily", "never", "Mon,Tue,..."
    seen_weekdays: frozenset[int]
    observed_days: int              # days it actually wrote
    every_weekday_twice: bool       # trust guard input
    multi_daily: bool
    median_runs: int
```

`_history_summary` returns a `ReadSummary`. `_verdict_for` computes the window:

- `daily` → `fresh_hours` (unchanged).
- `never` → unchanged rules (no qualifying write blocks).
- any other trusted cadence → `(max_gap_days(seen) + 1) * fresh_hours`.
- `mode == "weekly"` → force `max_gap_days = 7`, whatever the observed cadence.
- `check_multi_daily` and `multi_daily` → the effective window is `window // median_runs`, on top of the above.

**Trust guard (fail closed):** the cadence extension applies only when `observed_days >= 10` AND `every_weekday_twice`. Otherwise the table keeps the strict daily window. A new table cannot relax its own check.

**Label rule (`_weekday_label`):**
- Remove the `len(seen) >= 6 -> "daily"` fallback. A Mon–Sat table gets `"Mon,Tue,Wed,Thu,Fri,Sat"`.
- `"daily"` now requires: all possible weekdays seen, AND `len(days) >= 0.9 * len(own_span)`. `own_span` is the contiguous range from the table's first write to its last write, not the window span. A table created mid-window still reads as daily if it wrote every day of its own life.
- New signature: `_weekday_label(days, span, own=None)`; `own` defaults to `span`. `dependency_rows` and `_history_summary` pass the table's own span.

**`ignore_non_daily`:** keeps only one behavior — a table with no write in history passes as a note. Its cadence behavior is now the default. The hardcoded 168h window is deleted. Say this in the README and in HANDOFF 11.

**Report wording:** a schedule-checked dependency prints `schedule: <label> — window <N>h` when fresh, and `schedule: <label> — older than the <N>h window` when stale. `N` is the computed window, so Mondays-only prints 192, not 168.

- [ ] **Step 1: Write the failing label tests**

In `tests/test_recommend.py`, three tests:

```python
def test_mon_to_sat_is_not_daily():
    # seen = 6 of 7 weekdays, coverage 26 of 30. The >= 6 fallback must go.
    # Expected label: "Mon,Tue,Wed,Thu,Fri,Sat".

def test_a_table_created_mid_window_is_still_daily():
    # Writes every day from day 10 of a 30-day window. own-span coverage
    # is 20 of 20. Expected label: "daily".

def test_gap_days_computes_the_cyclic_gap():
    assert max_gap_days(frozenset({0, 1, 2, 3, 4})) == 3   # Fri -> Mon
    assert max_gap_days(frozenset({0})) == 7               # Mondays only
    assert max_gap_days(frozenset({0, 1, 2, 3, 4, 5})) == 2  # Sat -> Mon
```

- [ ] **Step 2: Run them, confirm they fail**

Run: `python -m pytest tests/test_recommend.py -v`
Expected: FAIL — Mon–Sat reads "daily"; mid-window table reads as a joined label; `max_gap_days` does not exist.

- [ ] **Step 3: Implement the label and gap changes in `recommend.py`**

- [ ] **Step 4: Write the failing gate tests**

In `tests/test_gate.py`. Use the existing fake-Workspace seam. Pin `now=` in every test. Times in `Asia/Kolkata`.

```python
def test_mon_to_fri_table_is_fresh_on_monday_morning():
    # Writes Mon–Fri at 04:00 for 3 weeks. now = Monday 06:00.
    # Last write Friday 04:00 = 74h old. Window (3+1)*24 = 96h. READY.

def test_a_missed_friday_is_caught_on_monday():
    # Same table, but this Friday never wrote. now = Monday 06:00.
    # Last write Thursday 04:00 = 98h > 96h. BLOCKED.

def test_mondays_only_table_is_fresh_on_monday_morning():
    # Writes Mondays at 04:00. now = Monday 06:00, this Monday has not
    # run yet. Last write = 170h < 192h. READY.

def test_a_missed_monday_is_caught_on_tuesday():
    # Same table, this Monday never wrote. now = Tuesday 06:00.
    # Last write = 194h > 192h. BLOCKED.

def test_a_sparse_table_gets_no_cadence_extension():
    # A table observed on 3 days only, last write 30h ago. BLOCKED.
    # The trust guard keeps the daily window.

def test_one_weekday_seen_once_gets_no_cadence_extension():
    # 12 observed days, but Tuesday appears once and a Tuesday is the
    # freshest write. BLOCKED. The trust guard fails closed.

def test_mode_weekly_forces_the_weekly_gap():
    # A table marked 'view_scheduler.mode' = 'weekly'. Window = 8 * 24.

def test_check_multi_daily_shortens_the_computed_window():
    # A Mon–Fri table that runs 2x a day. Window = 96 // 2 = 48h.
```

- [ ] **Step 5: Run them, confirm they fail**

Run: `python -m pytest tests/test_gate.py -v`
Expected: FAIL — Monday morning blocks; the 168h strings do not match.

- [ ] **Step 6: Implement in `gate.py`**

`ReadSummary`, the new `_history_summary` return, and the window logic in `_verdict_for`. Delete the 168h constant. `Dependency.detail` wording per the Interfaces block.

- [ ] **Step 7: Run the full suite; update goldens deliberately**

Run: `python -m pytest -q`
Expected: `test_ignore_non_daily_blocks_a_weekly_table_older_than_168h` and the "schedule: Mons" goldens change. Retarget them to the computed window (192h). Do not weaken assertions.

- [ ] **Step 8: Update the docs in the same commit**

`README.md` gate section: the decision table row for streaming stays; add the cadence rule and the trust guard in plain words; rewrite "Options that relax the daily check" — `ignore_non_daily` now covers only the no-history case. `HANDOFF.md` 10 (new domain fact: the Monday rule), 11 (decision reversal, dated 2026-10-16), 15 (nothing changes there).

- [ ] **Step 9: Commit**

```bash
git add src/view_scheduler/recommend.py src/view_scheduler/gate.py tests/test_recommend.py tests/test_gate.py README.md HANDOFF.md
git commit -m "Gate checks each table against its own observed schedule"
```

---

### Task 3: No headline on unreadable evidence

**Files:**
- Modify: `src/view_scheduler/models.py` (`Skipped`)
- Modify: `src/view_scheduler/walker.py` (function notes pass `blocks=False`)
- Modify: `src/view_scheduler/run.py` (suppress the times)
- Modify: `src/view_scheduler/report.py` (the "could not be read" branch)
- Test: `tests/test_advice.py`, `tests/test_report.py`

**Interfaces:**
- Produces: `Skipped(name: str, reason: str, blocks: bool = True)`. A function dependency note constructs with `blocks=False`. Everything else keeps the default.
- Produces: when any skip has `blocks=True`, `collect_history` nulls the recommendation's four time fields (`safe`, `safe_without_outliers`, `usual`, `earlier`) and the three `*_next_day` flags, via `dataclasses.replace`. Rows and `all_clear` stay, so the report still shows the readable evidence.
- Produces: report wording, when blocked by an unreadable dependency:

```
       NO TIME TO SUGGEST — 1 dependency could not be read.
       The times below cover only the 8 dependencies that were read.
```

Then the dependency table and the day-by-day series print as today.

**Scope:** the gate does not change. A function dependency (a UDF note) never suppresses the headline.

- [ ] **Step 1: Write the failing tests**

In `tests/test_advice.py`:

```python
def test_an_unreadable_dependency_removes_the_headline():
    # A view with 2 readable tables and 1 table whose DESCRIBE HISTORY
    # raises a permission error.
    assert advice.safe_time is None
    assert advice.usual_time is None
    assert "could not be read" in advice.text
    assert advice.to_dict()["safe_time"] is None

def test_a_function_dependency_keeps_the_headline():
    # A view whose only skip is a UDF note. SCHEDULE AT still prints,
    # and the note still appears in warnings.
```

- [ ] **Step 2: Run them, confirm they fail**

Run: `python -m pytest tests/test_advice.py -v`
Expected: FAIL — the headline stands today.

- [ ] **Step 3: Implement**

`blocks` on `Skipped`; `blocks=False` at the two function-note sites in `walker.py`; the `dataclasses.replace` in `run.py`; the report branch in `report.py`.

- [ ] **Step 4: Run the full suite; update goldens deliberately**

Run: `python -m pytest -q`
Expected: any advise golden with a skip now shows the new branch. Retarget, do not weaken.

- [ ] **Step 5: Update the README in the same commit**

The advise section: "Nothing is dropped silently" stays true, and add: an unreadable dependency removes the headline, because a time computed without one input is not a safe time.

- [ ] **Step 6: Commit**

```bash
git add src/view_scheduler/models.py src/view_scheduler/walker.py src/view_scheduler/run.py src/view_scheduler/report.py tests/test_advice.py tests/test_report.py README.md
git commit -m "No advised time while any dependency is unreadable"
```

---

### Task 4: Write the first-commit tradeoff down

**Files:**
- Modify: `README.md` (new subsection under "The report: three times, not two")
- Modify: `HANDOFF.md` (section 10, new domain fact)
- Test: `tests/test_history.py` (one pin test)

**Interfaces:** none. No code change. The pin test documents today's behavior so it cannot change by accident.

- [ ] **Step 1: Write the pin test (it passes immediately — that is the point)**

In `tests/test_history.py`:

```python
def test_the_first_commit_of_the_day_is_the_finish():
    """A day with two qualifying commits (04:00 staging, 05:00 final)
    reports 04:00. This is deliberate: a one-off rerun at 22:00 must not
    move the schedule. A job that commits twice per run needs a bigger
    buffer. See README, 'Which commit counts as the finish'."""
    # commits at 04:00 and 05:00 on one day
    assert finish_times(commits, today, 30, tz)[day].hour == 4
```

- [ ] **Step 2: Run it, confirm it passes**

Run: `python -m pytest tests/test_history.py::test_the_first_commit_of_the_day_is_the_finish -v`
Expected: PASS. If it fails, the behavior changed by accident — stop and investigate before writing any doc.

- [ ] **Step 3: Write the README subsection, in ASD-STE**

Title: "Which commit counts as the finish". Content, in this order: DESCRIBE HISTORY gives commit moments only; the first commit of the local day counts; this is right for reruns; this is wrong for a job that commits twice; raise `--buffer` when a job commits twice per run; a table that commits twice on a typical day is flagged `multi_daily`.

- [ ] **Step 4: Add the domain fact to HANDOFF section 10**

Same content, one paragraph, numbered as the next fact in the list.

- [ ] **Step 5: Commit**

```bash
git add README.md HANDOFF.md tests/test_history.py
git commit -m "Say which commit counts as the finish, and why"
```

**Open question for the owner, not a task:** add a rig probe table that commits twice per run, and check what the advisor says. Rig changes touch live fixtures. The fixture determinism rule applies. Decide after the current rig window closes.

---

### Task 5: Retry transient read failures

**Files:**
- Modify: `src/view_scheduler/client.py` (`is_transient`, `with_retry`, `get_table`, `sql`)
- Modify: `src/view_scheduler/obs.py` (one `retry` log function)
- Test: `tests/test_client.py`
- Docs: `HANDOFF.md` (section 15, reversal), `README.md` (one line in "The two things most likely to bite" or the gate permissions note)

**Interfaces:**
- Produces in `client.py`:

```python
def is_transient(exc: Exception) -> bool: ...
def with_retry(fn, *, kind: str, target: str, attempts: int = 3,
               sleep=time.sleep): ...
```

- Produces in `obs.py`:

```python
def retry(kind: str, target: str, attempt: int, wait_s: float,
          error: str) -> None:
    """One JSON line per retry attempt."""
```

**Decision table for `is_transient` (pin all rows in tests):**

| Input | Transient |
|---|---|
| HTTP 429, 500, 502, 503, 504 (read from the exception's status code attribute when present) | yes |
| Error text: "rate limit", "temporarily unavailable", "connection", "timed out", "timeout", "canceled" | yes |
| Permission, 401, 403, not found, 404 | no — fail closed, at once |
| `expect_table_not_view` (the MV rejection) | no — it is the fallback trigger, not a fault |
| A SQL syntax or missing-table error from the statement API | no |

**Retry policy:** 3 attempts total. Waits 1.0s, then 2.0s. No jitter in 0.7.0 — tests stay deterministic. If the exception exposes a retry-after value, wait the larger of that and the computed wait.

**Where it wraps:** the SDK HTTP call in `get_table`, and the statement submission plus every result-chunk fetch in `sql`. The Spark path gets no retry — local compute, a failure there is real. `read_commits` and its MV fallback are unchanged: the rejection inside them is permanent, so it falls through at once.

**HANDOFF reversal:** section 15 says "No concurrency or 429 backoff". Strike the backoff half. Record the reversal and the date. Concurrency stays out.

- [ ] **Step 1: Write the failing tests**

In `tests/test_client.py`. Use the existing fake seam. Inject a `sleep` recorder; no real sleeping.

```python
def test_a_transient_error_is_retried_and_then_succeeds():
    # get_table raises a fake 503 twice, then returns metadata.
    # sleeps recorded == [1.0, 2.0]; the metadata comes back.

def test_a_permanent_error_is_not_retried():
    # get_table raises a fake 403 once. It raises out at once.
    # sleeps recorded == [].

def test_the_mv_rejection_is_not_retried():
    # read_commits on an MV: the DESCRIBE rejection falls back to the
    # event log on the first attempt.

def test_every_retry_logs_one_line():
    # Two retries produce two obs lines with attempt=1 and attempt=2.
```

- [ ] **Step 2: Run them, confirm they fail**

Run: `python -m pytest tests/test_client.py -v`
Expected: FAIL — `with_retry` does not exist; the 503 raises out at once.

- [ ] **Step 3: Implement `is_transient`, `with_retry`, and `obs.retry`**

One code note: read the status from `getattr(exc, "status_code", None)` first. Fall back to the word list only when there is no status.

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS. No existing test sleeps for real — if one does, the injected `sleep` seam was missed somewhere.

- [ ] **Step 5: Update the docs in the same commit**

`HANDOFF.md` 15: strike "or 429 backoff", note the reversal dated 2026-10-16, note concurrency stays serial. `README.md`: one line under the gate section — "A read that fails for a passing reason (rate limit, a passing warehouse error) is tried again, twice. A read that fails for a lasting reason blocks at once."

- [ ] **Step 6: Commit**

```bash
git add src/view_scheduler/client.py src/view_scheduler/obs.py tests/test_client.py HANDOFF.md README.md
git commit -m "Retry transient reads: 3 attempts, fail closed at once"
```

---

### Task 6: Release cut, 0.7.0

**Files:**
- Modify: `pyproject.toml` (version)
- Modify: `README.md` (truth pass), `docs/01-plan.html` (cut-list rows)
- Commit: the untracked docs

**Interfaces:** none.

- [ ] **Step 1: Fix the README's `earlier_time` claim**

`advice.py` nulls `earlier_time` when it equals `safe_time`. The README says it "always equals `advice.usual_time`". Rewrite the sentence: "It is `None` when it equals the SCHEDULE AT time. `advice.usual_time` carries the value in every case."

- [ ] **Step 2: Fix the plan doc's cut-list**

`docs/01-plan.html`: "Ordinal slot model" and "Coverage tiers, order statistics" shipped in 0.6.0. Mark both "Already back (0.6.0)", as the "Tolerance bands" row already is.

- [ ] **Step 3: Commit the untracked docs**

`git add docs/05-interactive-guide.html docs/06-code-walkthrough.html docs/07-flow-map.md notebooks/view_scheduler_v1_vs_v2.py superpowers-explainer.html` — review each file once before adding it. The README links two of them; today they 404 on GitHub.

- [ ] **Step 4: Bump the version, build, verify the wheel**

`pyproject.toml` version to 0.7.0. Then:

```bash
python -m build --wheel
python -m zipfile -l dist/view_scheduler-0.7.0-py3-none-any.whl
```

Expected: only `view_scheduler/` entries. Then install into a clean virtual environment and import `advise`, `can_refresh`, `analyze`. (HANDOFF 13.)

- [ ] **Step 5: Run the full suite one last time**

Run: `python -m pytest`
Expected: all pass. Record the count in the README's Development section.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml README.md docs/01-plan.html
git commit -m "Release 0.7.0: cadence-aware gate, honest headline, retries"
```

---

## Self-review notes

- Spec coverage: owner decisions 2, 3, 4, 5, 6 map to Tasks 2, 3, 1, 4, 5. Decision 1 needs no task.
- Type consistency: `ReadSummary` replaces the 4-tuple `_history_summary` returns today; `hist_cache` in `can_refresh` types change with it, in the same task.
- Proportion: five behavior tasks, one release task. The bodies that matter are the decision tables; the rest is signatures and tests.
