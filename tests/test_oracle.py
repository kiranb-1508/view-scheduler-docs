"""Offline exercise of the rig's oracle and the scenarios it validates.

Two things live here, both runnable without a warehouse:

  * the recommend/report behaviours the roster was specifically built to expose,
    driven by the real recommend() over the plan's finishes; and
  * the mismatches() comparison harness that check.py depends on at day 30.

What is NOT here, deliberately: a test asserting "the oracle agrees with the
analyzer" on plan data. After the oracle was collapsed onto the real recommend(),
both sides of that comparison are recommend() over the same plan finishes, so it
is true by construction. The genuine differential — do real finish times match
the plan — can only happen against the live rig, which is what check.py does.
"""

from datetime import datetime, time, timedelta

import pytest

import oracle
import plan
from view_scheduler.models import TableHistory
from view_scheduler.recommend import recommend
from view_scheduler.report import render

TODAY = plan.RIG_START + timedelta(days=29)
BUFFER = 15


def plan_history(table: str, shift_min: int = 0, prefix: str = "",
                 drop: set | None = None) -> TableHistory:
    """A TableHistory from the plan's finishes, optionally shifted later (to
    simulate a cold start), renamed (as the analyzer reports), or with days
    dropped (to simulate a missed refresh)."""
    drop = drop or set()
    finishes = oracle.finishes_upto(table, TODAY, 30)
    return TableHistory(
        name=prefix + table,
        finish_times={
            day: datetime.combine(day, oracle.slot_time(slot), tzinfo=oracle.TZ)
                 + timedelta(minutes=shift_min)
            for day, slot in finishes.items() if day not in drop},
        metrics_degraded=False,
    )


def history_from_plan(table: str) -> TableHistory:
    return plan_history(table)


# --- the behaviours the roster was built to prove ----------------------------

def test_late_tail_pushes_safe_out_and_leaves_earlier_behind():
    """The two 06:10 days are why safe and earlier differ at all. Without them
    every day finishes at 04:00 and the two numbers collapse together."""
    result = recommend([history_from_plan("t_daily_late_tail")], BUFFER)
    assert result.safe == time(6, 25)
    assert result.safe_without_outliers == time(4, 15)
    assert result.earlier == time(4, 15), \
        "both late days are outliers, so earlier falls back to the normal tail"

    tight = recommend([history_from_plan("t_daily_tight")], BUFFER)
    assert tight.safe == tight.earlier == time(4, 15)


def test_a_weekly_dependency_hides_the_other_tables_bad_days():
    """Worth knowing about, and not obvious.

    v_realistic contains both the late-tail table and a Mondays-only table. The
    intersection keeps only days where everything refreshed, so the evidence
    collapses to the Mondays — and neither of the late-tail's two bad days is a
    Monday. The view therefore recommends 04:15 as if the late days never
    happened; the n_days figure is the only hint that the evidence shrank.
    """
    tables = plan.VIEWS["v_realistic"]
    result = recommend([history_from_plan(t) for t in tables], BUFFER)
    mondays = len(oracle.finishes_upto("t_weekly_mon", TODAY, 30))
    assert result.safe == time(4, 15)
    assert result.n_days == mondays

    late_days = {d for d, slot in plan.expected_finishes("t_daily_late_tail").items()
                 if slot == "06:10"}
    assert not any(d.weekday() == 0 for d in late_days)

    # Drop the weekly table and the late days reappear.
    without_weekly = [t for t in tables if t != "t_weekly_mon"]
    result2 = recommend([history_from_plan(t) for t in without_weekly], BUFFER)
    assert result2.safe == time(6, 25) and result2.n_days > 20


def test_healthy_view_prints_no_warnings_at_all():
    tables = plan.VIEWS["v_healthy"]
    result = recommend([history_from_plan(t) for t in tables], BUFFER)
    text = render("v_healthy", result, [], "Asia/Kolkata")
    assert "!" not in text, text


def test_broken_view_refuses_to_invent_a_time():
    tables = plan.VIEWS["v_broken"]
    histories = [history_from_plan(t) for t in tables if t in plan.DRIVEN_TABLES]
    histories.append(TableHistory("t_dormant", {}, False))
    result = recommend(histories, BUFFER)
    text = render("v_broken", result, [], "Asia/Kolkata")
    assert result.safe is None
    assert "NO TIME TO SUGGEST" in text and "SCHEDULE AT" not in text


def test_midnight_view_keeps_both_sides_of_the_boundary_on_their_own_days():
    tables = plan.VIEWS["v_midnight"]
    result = recommend([history_from_plan(t) for t in tables], BUFFER)
    assert result.n_days == 30, "every day has both a 00:20 and a 23:40 commit"
    assert result.safe == time(23, 55), "the 23:40 table sets the all-clear"


# --- the mismatches() harness that check.py trusts at day 30 -----------------

REALISTIC = plan.VIEWS["v_realistic"]
NAMES = {t: f"c.s.{t}" for t in REALISTIC}


def actual(shift_min: int = 0, drop: dict | None = None):
    drop = drop or {}
    return recommend([plan_history(t, shift_min=shift_min, prefix="c.s.",
                                   drop=drop.get(t)) for t in REALISTIC], BUFFER)


def test_mismatches_is_empty_when_the_report_matches_the_plan():
    expected = oracle.expected_recommendation(REALISTIC, TODAY, BUFFER, 30)
    assert oracle.mismatches(expected, actual(), NAMES) == []


def test_mismatches_tolerates_a_cold_start_delay():
    """Every finish 20 minutes late — inside the 25-minute tolerance — must not
    register as a disagreement."""
    expected = oracle.expected_recommendation(REALISTIC, TODAY, BUFFER, 30)
    assert oracle.mismatches(expected, actual(shift_min=20), NAMES) == []


def test_mismatches_flags_a_delay_beyond_tolerance():
    expected = oracle.expected_recommendation(REALISTIC, TODAY, BUFFER, 30)
    problems = oracle.mismatches(expected, actual(shift_min=40), NAMES)
    assert any("safe" in p for p in problems)


def test_mismatches_flags_a_missing_refresh_day():
    """Drop one day from a daily table and its missed count must diverge."""
    expected = oracle.expected_recommendation(REALISTIC, TODAY, BUFFER, 30)
    a_monday = next(d for d in plan.days() if d.weekday() == 0)
    problems = oracle.mismatches(expected, actual(drop={"t_daily_tight": {a_monday}}),
                                 NAMES)
    assert any("t_daily_tight" in p and "missed" in p for p in problems)
