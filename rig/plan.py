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
RIG_START = date(2026, 8, 31)
RIG_DAYS = 30
RIG_END = RIG_START + timedelta(days=RIG_DAYS - 1)

#: Slots, in Asia/Kolkata. The job schedules use these exact strings.
SLOTS = ["00:20", "02:00", "04:00", "06:10", "09:00", "17:00", "23:40"]

#: How late after its slot a commit may land and still count as that slot.
#: Serverless cold starts are the reason this is minutes rather than seconds.
SLOT_TOLERANCE_MIN = 25

OVERWRITE, APPEND, MERGE, TOUCH = "overwrite", "append", "merge", "touch"


@dataclass(frozen=True)
class Write:
    """One statement the rig will issue."""
    table: str
    kind: str
    rows: int = 0

    @property
    def qualifies(self) -> bool:
        """Whether the scheduler should count this as a refresh.

        Mirrors the rules in scheduler.history, stated independently here so
        the oracle is not merely echoing the code under test.
        """
        if self.kind == TOUCH:
            return False              # metadata only, never a refresh
        if self.kind == OVERWRITE:
            return True               # a rebuild that produced nothing still ran
        return self.rows > 0          # append and merge need rows


def days() -> list[date]:
    return [RIG_START + timedelta(days=i) for i in range(RIG_DAYS)]


def is_active(day: date) -> bool:
    return RIG_START <= day <= RIG_END


def die(table: str, day: date, salt: str = "", n: int = 100) -> int:
    """A stable pseudo-random integer in [0, n) for this table and day."""
    key = f"{table}|{day.isoformat()}|{salt}".encode()
    return int(hashlib.md5(key).hexdigest()[:8], 16) % n


def _pick(table: str, salt: str, window: list[date], count: int) -> set[date]:
    """The `count` lowest-ranked days within `window` — exactly that many,
    chosen deterministically, rather than a probability that might give three
    or one."""
    ranked = sorted(window, key=lambda d: (die(table, d, salt, 10 ** 6), d))
    return set(ranked[:count])


#: Deliberately spread across the window rather than left to the hash. Ranked
#: purely by hash, both late days and all three skips landed in the final week,
#: which would leave the early checkpoints with nothing to detect.
LATE_DAYS = (_pick("t_daily_late_tail", "late", days()[2:9], 1)
             | _pick("t_daily_late_tail", "late", days()[9:], 1))

FLAKY_SKIPS = (_pick("t_flaky", "skip", days()[1:8], 1)
               | _pick("t_flaky", "skip", days()[8:], 2))


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
        # Zero rows on a third of days. The append must not qualify; the
        # overwrite must, and that asymmetry is the whole test.
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
