"""The write matrix. Pure, deterministic, and the specification the oracle reads.

Every decision the rig makes — whether a table writes on a given day, in which
slot, with how many rows — is a function of (table, date) through md5. Nothing
here reads the wall clock, the network, or any state.

That determinism is the point: the same module tells the writer what to do and
tells the oracle what to expect, so on day 30 the analyzer's output can be
asserted rather than eyeballed.

Python's built-in hash() is salted per process and would break this. Only
hashlib is used.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, timedelta

SCHEMA = "workspace.sched_rig"

#: First and last day the rig writes, inclusive. 30 days.
# Starts the day after deployment. The smoke-test run used to prove the job
# works wrote outside its slot, and those commits would not match the oracle;
# beginning at midnight means all 30 days come from real scheduled runs.
RIG_START = date(2026, 9, 1)
RIG_DAYS = 30
RIG_END = RIG_START + timedelta(days=RIG_DAYS - 1)

#: Slots, in Asia/Kolkata. The job schedules use these exact strings.
SLOTS = ["00:20", "02:00", "04:00", "06:10", "09:00", "17:00", "23:40"]

#: How late after its slot a commit may land and still count as that slot.
#: Serverless cold starts are the reason this is minutes rather than seconds.
SLOT_TOLERANCE_MIN = 25

OVERWRITE, APPEND, MERGE, TOUCH = "overwrite", "append", "merge", "touch"


def guard(statement: str, allow: tuple[str, ...] = ()) -> None:
    """Refuse any statement that does not stay inside the rig schema.

    The single enforcement point both the setup DDL and the slot writer call:
    the rig only ever writes to SCHEMA. `allow` names extra scratch objects the
    caller may legitimately touch (setup drops a leftover capability-probe
    schema).
    """
    if SCHEMA in statement or any(token in statement for token in allow):
        return
    raise SystemExit(f"refusing statement outside {SCHEMA}:\n  {statement}")


@dataclass(frozen=True)
class Write:
    """One statement the rig will issue."""
    table: str
    kind: str
    rows: int = 0

    @property
    def qualifies(self) -> bool:
        """Whether the scheduler should count this as a refresh.

        Mirrors the rule in scheduler.history: any write counts — a full replace
        or an incremental — regardless of row count. A zero-row append still
        means the pipeline ran that day. Only the metadata-only TOUCH is not a
        refresh.
        """
        return self.kind != TOUCH


def days() -> list[date]:
    return [RIG_START + timedelta(days=i) for i in range(RIG_DAYS)]


def is_active(day: date) -> bool:
    return RIG_START <= day <= RIG_END


def die(table: str, day: date, n: int = 100) -> int:
    """A stable pseudo-random integer in [0, n) for this table and day.

    Used for the per-day zero-row decision, which genuinely needs to vary
    across the window. The two outlier sets below are fixed, so they are just
    literal day offsets rather than a ranked hash.
    """
    key = f"{table}|{day.isoformat()}".encode()
    return int(hashlib.md5(key).hexdigest()[:8], 16) % n


#: Spread across the window on purpose so each checkpoint sees signal: one late
#: day and one skip land before day 8, the rest later. These are offsets from
#: RIG_START — 2026-09-08 / -24 late, 2026-09-05 / -16 / -25 skipped — and the
#: scheduled checkpoint prompts name those exact dates, so do not move them
#: without updating those.
LATE_DAYS = {days()[7], days()[23]}
FLAKY_SKIPS = {days()[4], days()[15], days()[24]}


def writes_for(slot: str, day: date) -> list[Write]:
    """Everything to write in this slot on this day."""
    if not is_active(day) or slot not in SLOTS:
        return []

    out: list[Write] = []
    index = (day - RIG_START).days

    if slot == "00:20":
        out.append(Write("t_near_midnight", OVERWRITE, 50))

    if slot == "02:00":
        out.append(Write("t_multi_daily", APPEND, 5))

    if slot == "04:00":
        out.append(Write("t_daily_tight", OVERWRITE, 100))
        out.append(Write("t_daily_append", APPEND, 10))
        # Zero rows on a third of days, for both the append and the overwrite.
        # Both must still qualify: a zero-row write means the pipeline ran on a
        # quiet day. The pair proves the row count does not change the verdict,
        # only the class.
        zero = die("t_zero", day) % 3 == 0
        out.append(Write("t_append_zero", APPEND, 0 if zero else 10))
        out.append(Write("t_overwrite_zero", OVERWRITE, 0 if zero else 10))
        out.append(Write("t_merge", MERGE, 8))

        if day.weekday() == 0:
            out.append(Write("t_weekly_mon", OVERWRITE, 30))
        if day not in FLAKY_SKIPS:
            out.append(Write("t_flaky", OVERWRITE, 20))
        if day not in LATE_DAYS:
            out.append(Write("t_daily_late_tail", OVERWRITE, 40))

        # A real refresh every third day; a metadata-only commit otherwise.
        # SET TBLPROPERTIES always commits, where OPTIMIZE on a small table may
        # find nothing to compact and not commit at all — and a day with no
        # commit would not prove that maintenance is correctly ignored.
        if index % 3 == 0:
            out.append(Write("t_maintenance_only", OVERWRITE, 15))
        else:
            out.append(Write("t_maintenance_only", TOUCH))

    if slot == "06:10" and day in LATE_DAYS:
        out.append(Write("t_daily_late_tail", OVERWRITE, 40))

    if slot == "09:00":
        out.append(Write("t_multi_daily", APPEND, 5))

    if slot == "17:00":
        out.append(Write("t_multi_daily", APPEND, 5))

    if slot == "23:40":
        out.append(Write("t_late_evening", OVERWRITE, 50))

    return out


# --- what the analyzer should see -------------------------------------------

def expected_finishes(table: str) -> dict[date, str]:
    """{day: slot} — the first qualifying commit of each day, per the plan.

    This is what scheduler.history.finish_times should reproduce from the real
    commit log.
    """
    out: dict[date, str] = {}
    for day in days():
        for slot in SLOTS:                     # SLOTS is in chronological order
            if any(w.table == table and w.qualifies for w in writes_for(slot, day)):
                out[day] = slot
                break
    return out


#: Tables the slot writer drives. The MV and streaming table run on their own
#: schedules and are not listed here.
DRIVEN_TABLES = [
    "t_daily_tight", "t_daily_late_tail", "t_daily_append", "t_append_zero",
    "t_overwrite_zero", "t_merge", "t_weekly_mon", "t_flaky",
    "t_maintenance_only", "t_multi_daily", "t_near_midnight", "t_late_evening",
]

#: Written once at setup, never again.
DORMANT_TABLES = ["t_dormant"]

#: Views the report is exercised against.
VIEWS = {
    "v_healthy": ["t_daily_tight", "t_daily_append"],
    "v_realistic": ["t_daily_tight", "t_daily_late_tail", "t_weekly_mon", "t_flaky"],
    "v_midnight": ["t_near_midnight", "t_late_evening"],
    "v_broken": ["t_dormant", "t_maintenance_only"],
}
