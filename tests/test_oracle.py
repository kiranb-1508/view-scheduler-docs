"""Run the full 30-day validation today, against histories built from the plan.

This is the day-30 gate, rehearsed. The only thing that changes on day 30 is
where the finish times come from: real DESCRIBE HISTORY instead of the plan. If
the analyzer disagrees with the oracle here, it will disagree there too — and
finding that out now is worth thirty days.
"""

import sys
from datetime import datetime, time

import pytest

sys.path.insert(0, "rig")

import oracle  # noqa: E402
import plan  # noqa: E402
from scheduler.models import TableHistory  # noqa: E402
from scheduler.recommend import recommend  # noqa: E402
from scheduler.report import render  # noqa: E402

TODAY = plan.RIG_END
BUFFER = 15


def history_from_plan(table: str) -> TableHistory:
    """Exactly the finish times the rig will have produced, at slot precision."""
    finishes = oracle.finishes_upto(table, TODAY, 30)
    return TableHistory(
        name=table,
        finish_times={day: datetime.combine(day, oracle.slot_time(slot),
                                            tzinfo=oracle.TZ)
                      for day, slot in finishes.items()},
        window_days=30,
        metrics_degraded=False,
    )


def check(tables: list[str]) -> list[str]:
    actual = recommend([history_from_plan(t) for t in tables], BUFFER)
    return oracle.mismatches(oracle.expected(tables, TODAY, BUFFER), actual)


@pytest.mark.parametrize("view", sorted(plan.VIEWS))
def test_analyzer_agrees_with_the_oracle_on_every_view(view):
    assert check(plan.VIEWS[view]) == []


@pytest.mark.parametrize("table", plan.DRIVEN_TABLES)
def test_analyzer_agrees_with_the_oracle_on_every_table_alone(table):
    assert check([table]) == []


# --- the specific behaviours the roster was built to prove -------------------

def test_late_tail_pushes_safe_out_and_leaves_tighter_behind():
    """The two 06:10 days are why safe and tighter differ at all. Without them
    every day finishes at 04:00 and the two numbers collapse together."""
    result = recommend([history_from_plan("t_daily_late_tail")], BUFFER)
    assert result.safe == time(6, 25)
    assert result.tighter == time(6, 25), "two late days, so second-worst is also late"

    tight = recommend([history_from_plan("t_daily_tight")], BUFFER)
    assert tight.safe == tight.tighter == time(4, 15)


def test_a_weekly_dependency_hides_the_other_tables_bad_days():
    """Worth knowing about, and not obvious.

    v_realistic contains both the late-tail table and a Mondays-only table. The
    intersection keeps only days where everything refreshed, so the evidence
    collapses to five Mondays — and neither of the late-tail's two bad days is a
    Monday. The view therefore recommends 04:15 as if the late days never
    happened.

    That is the arithmetic behaving correctly, but it means a weekly dependency
    quietly shrinks the evidence behind every other dependency's number. The
    n_days figure in the report is what tells a reader this happened.
    """
    tables = plan.VIEWS["v_realistic"]
    result = recommend([history_from_plan(t) for t in tables], BUFFER)
    mondays = len([d for d in plan.days() if d.weekday() == 0])
    assert result.safe == time(4, 15)
    assert result.n_days == mondays

    late_days = {d for d, slot in plan.expected_finishes("t_daily_late_tail").items()
                 if slot == "06:10"}
    assert not any(d.weekday() == 0 for d in late_days)

    # Drop the weekly table and the late days reappear.
    without_weekly = [t for t in tables if t != "t_weekly_mon"]
    result2 = recommend([history_from_plan(t) for t in without_weekly], BUFFER)
    assert result2.safe == time(6, 25) and result2.n_days > 20


def test_zero_row_appends_show_up_as_missed_days():
    result = recommend([history_from_plan("t_append_zero")], BUFFER)
    row = result.rows[0]
    assert row.missed > 0, "days whose only write appended nothing must count as missed"


def test_zero_row_overwrites_do_not():
    result = recommend([history_from_plan("t_overwrite_zero")], BUFFER)
    assert result.rows[0].missed == 0
    assert result.n_days == 30


def test_weekly_table_reads_as_mondays_with_nothing_missed():
    result = recommend([history_from_plan("t_weekly_mon")], BUFFER)
    assert result.rows[0].refreshes == "Mons"
    assert result.rows[0].missed == 0


def test_flaky_table_reports_its_three_skipped_days():
    result = recommend([history_from_plan("t_flaky")], BUFFER)
    assert result.rows[0].refreshes == "daily"
    assert result.rows[0].missed == 3


def test_maintenance_only_days_are_missing_not_refreshed():
    result = recommend([history_from_plan("t_maintenance_only")], BUFFER)
    assert result.n_days == 10, "a metadata-only commit is not a refresh"


def test_intersection_drops_days_where_any_dependency_was_absent():
    """v_realistic pairs a Mondays-only table with three daily ones, so only
    Mondays survive the intersection."""
    tables = plan.VIEWS["v_realistic"]
    result = recommend([history_from_plan(t) for t in tables], BUFFER)
    assert result.n_days == len([d for d in plan.days() if d.weekday() == 0])
    assert all(d.weekday() == 0 for d in result.all_clear)


def test_healthy_view_prints_no_warnings_at_all():
    tables = plan.VIEWS["v_healthy"]
    result = recommend([history_from_plan(t) for t in tables], BUFFER)
    text = render("v_healthy", result, [], "Asia/Kolkata", 30)
    assert "!" not in text, text


def test_broken_view_refuses_to_invent_a_time():
    tables = plan.VIEWS["v_broken"]
    histories = [history_from_plan(t) for t in tables if t in plan.DRIVEN_TABLES]
    histories.append(TableHistory("t_dormant", {}, 30, False))
    result = recommend(histories, BUFFER)
    text = render("v_broken", result, [], "Asia/Kolkata", 30)
    assert result.safe is None
    assert "NO RECOMMENDATION" in text and "START AT" not in text


def test_midnight_view_keeps_both_sides_of_the_boundary_on_their_own_days():
    tables = plan.VIEWS["v_midnight"]
    result = recommend([history_from_plan(t) for t in tables], BUFFER)
    assert result.n_days == 30, "every day has both a 00:20 and a 23:40 commit"
    assert result.safe == time(23, 55), "the 23:40 table sets the all-clear"
