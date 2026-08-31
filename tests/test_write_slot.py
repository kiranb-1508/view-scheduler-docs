"""The writer emits the right SQL and refuses to leave its schema."""

import sys
from datetime import datetime, timedelta
from unittest import mock

import pytest

sys.path.insert(0, "rig")

import plan  # noqa: E402
import write_slot  # noqa: E402

DAY = plan.RIG_START


def test_every_planned_write_produces_valid_scoped_sql():
    for day in plan.days():
        for slot in plan.SLOTS:
            for write in plan.writes_for(slot, day):
                statement = write_slot.statement_for(write, day)
                assert plan.SCHEMA in statement
                write_slot.guard(statement)


def test_zero_row_writes_are_real_statements_not_skips():
    """A zero-row write must still commit. Skipping it would leave no commit at
    all, which is a different thing entirely from a commit that wrote nothing."""
    zero = plan.Write("t_append_zero", plan.APPEND, 0)
    assert "range(0)" in write_slot.statement_for(zero, DAY)


def test_overwrite_and_append_produce_different_delta_operations():
    over = write_slot.statement_for(plan.Write("t", plan.OVERWRITE, 5), DAY)
    app = write_slot.statement_for(plan.Write("t", plan.APPEND, 5), DAY)
    assert over.startswith("INSERT OVERWRITE") and app.startswith("INSERT INTO")


def test_touch_is_metadata_only():
    stmt = write_slot.statement_for(plan.Write("t", plan.TOUCH), DAY)
    assert "SET TBLPROPERTIES" in stmt
    assert "INSERT" not in stmt and "MERGE" not in stmt


def test_guard_rejects_anything_outside_the_rig_schema():
    with pytest.raises(SystemExit):
        write_slot.guard("INSERT OVERWRITE workspace.kiran_dev.seed_orders SELECT 1")


def test_unknown_write_kind_is_an_error_not_a_silent_skip():
    with pytest.raises(ValueError):
        write_slot.statement_for(plan.Write("t", "sneak"), DAY)


def test_before_the_window_the_task_does_nothing_and_pauses_nothing():
    """Deploying a day early must not stop the rig.

    is_active() is false on both sides of the window, so branching on it would
    have paused all seven jobs the first time the task ran before day 1 —
    killing the experiment silently, before it produced anything.
    """
    day_before = plan.RIG_START - timedelta(days=1)
    assert not plan.is_active(day_before)
    assert plan.writes_for("04:00", day_before) == []

    called = []
    original = write_slot.pause_all_rig_jobs
    write_slot.pause_all_rig_jobs = lambda: called.append(True)
    try:
        with mock.patch.object(write_slot, "datetime") as clock:
            clock.now.return_value = datetime(day_before.year, day_before.month,
                                              day_before.day, 4, 0, tzinfo=write_slot.TZ)
            assert write_slot.main(["--slot", "04:00"]) == 0
    finally:
        write_slot.pause_all_rig_jobs = original
    assert called == [], "the task paused the rig before the window opened"


def test_after_the_window_the_task_pauses_the_rig():
    day_after = plan.RIG_END + timedelta(days=1)
    called = []
    original = write_slot.pause_all_rig_jobs
    write_slot.pause_all_rig_jobs = lambda: called.append(True)
    try:
        with mock.patch.object(write_slot, "datetime") as clock:
            clock.now.return_value = datetime(day_after.year, day_after.month,
                                              day_after.day, 4, 0, tzinfo=write_slot.TZ)
            assert write_slot.main(["--slot", "04:00"]) == 0
    finally:
        write_slot.pause_all_rig_jobs = original
    assert called == [True], "the rig must stop itself once the window closes"
