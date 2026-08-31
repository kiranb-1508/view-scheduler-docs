"""The rig's write matrix is deterministic and shaped as the roster claims.

If these fail, the oracle is measuring something other than what the rig wrote.
"""

import subprocess
import sys
from datetime import date, timedelta

import pytest

import plan


def test_window_is_exactly_thirty_days():
    assert len(plan.days()) == 30
    assert plan.RIG_END - plan.RIG_START == timedelta(days=29)
    assert plan.is_active(plan.RIG_START) and plan.is_active(plan.RIG_END)
    assert not plan.is_active(plan.RIG_END + timedelta(days=1))


def test_the_matrix_is_stable_across_processes():
    """Python's hash() is salted per process. If it ever creeps in, the rig
    writes one thing and the oracle expects another, and the mismatch looks
    like an analyzer bug."""
    script = ("import sys; sys.path.insert(0, 'rig'); import plan; "
              "print(sorted(d.isoformat() for d in plan.LATE_DAYS), "
              "sorted(d.isoformat() for d in plan.FLAKY_SKIPS), "
              "[len(plan.expected_finishes(t)) for t in plan.DRIVEN_TABLES])")
    runs = {subprocess.run([sys.executable, "-c", script], capture_output=True,
                           text=True, check=True).stdout for _ in range(2)}
    assert len(runs) == 1, "the plan changed between processes"


def test_nothing_is_written_outside_the_window():
    for offset in (-1, plan.RIG_DAYS):
        day = plan.RIG_START + timedelta(days=offset)
        assert all(plan.writes_for(slot, day) == [] for slot in plan.SLOTS)


# --- the asymmetry the rig exists to prove -----------------------------------

def test_zero_row_overwrite_qualifies_but_zero_row_append_does_not():
    """Both tables take the same zero/non-zero decision on the same days. The
    overwrite table should show 30 refreshed days and the append table fewer —
    that gap is the design's rule made visible in real data."""
    overwrite = plan.expected_finishes("t_overwrite_zero")
    append = plan.expected_finishes("t_append_zero")
    assert len(overwrite) == 30
    assert 8 <= len(append) < 30, "zero-row appends must not count as refreshes"


def test_maintenance_only_days_are_not_refreshes():
    finishes = plan.expected_finishes("t_maintenance_only")
    assert len(finishes) == 10, "a real write every third day, and only those"
    for day in plan.days():
        writes = [w for w in plan.writes_for("04:00", day)
                  if w.table == "t_maintenance_only"]
        assert len(writes) == 1
        assert (day in finishes) == writes[0].qualifies


# --- per-table shapes --------------------------------------------------------

def test_weekly_table_only_runs_on_mondays():
    finishes = plan.expected_finishes("t_weekly_mon")
    assert finishes and all(d.weekday() == 0 for d in finishes)


def test_flaky_table_skips_exactly_three_days():
    assert len(plan.FLAKY_SKIPS) == 3
    assert len(plan.expected_finishes("t_flaky")) == 27


def test_late_tail_runs_late_on_exactly_two_days():
    assert len(plan.LATE_DAYS) == 2
    finishes = plan.expected_finishes("t_daily_late_tail")
    assert len(finishes) == 30
    assert sorted(d for d, slot in finishes.items() if slot == "06:10") == \
        sorted(plan.LATE_DAYS)


def test_outliers_are_spread_so_early_checkpoints_can_see_them():
    """Both late days and all three skips ranked into the final week when left
    to the hash, which would make the day-8 checkpoint blind."""
    first_ten = set(plan.days()[:10])
    assert plan.LATE_DAYS & first_ten
    assert plan.FLAKY_SKIPS & first_ten


def test_multi_daily_writes_three_times_but_the_first_slot_is_the_finish():
    day = plan.days()[0]
    slots = [s for s in plan.SLOTS
             if any(w.table == "t_multi_daily" for w in plan.writes_for(s, day))]
    assert slots == ["02:00", "09:00", "17:00"]
    assert plan.expected_finishes("t_multi_daily")[day] == "02:00"


def test_midnight_tables_sit_either_side_of_the_boundary():
    assert set(plan.expected_finishes("t_near_midnight").values()) == {"00:20"}
    assert set(plan.expected_finishes("t_late_evening").values()) == {"23:40"}


def test_dormant_table_is_never_written_by_any_slot():
    for day in plan.days():
        for slot in plan.SLOTS:
            assert not any(w.table == "t_dormant" for w in plan.writes_for(slot, day))


def test_every_driven_table_is_actually_driven():
    for table in plan.DRIVEN_TABLES:
        assert plan.expected_finishes(table), f"{table} never refreshes"


def test_views_only_reference_known_tables():
    known = set(plan.DRIVEN_TABLES) | set(plan.DORMANT_TABLES)
    for view, tables in plan.VIEWS.items():
        assert set(tables) <= known, view
