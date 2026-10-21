"""Phase 3 gate. The anchor is the worked example the design spec states
outright: safe 06:25, earlier 04:25, buffer 15."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from view_scheduler.models import TableHistory
from view_scheduler.recommend import (
    _outlier_dates,
    _weekday_label,
    dependency_rows,
    max_gap_days,
    recommend,
)
from tests import fixtures as f

IST = ZoneInfo("Asia/Kolkata")

MON, TUE, WED, THU, FRI = (date(2026, 8, d) for d in (3, 4, 5, 6, 7))
DAYS = [MON, TUE, WED, THU, FRI]


def history(name, times, degraded=False):
    """times: ['04:00', '04:10', ...] aligned to Mon..Fri; None = missed."""
    finish = {
        day: f.at(day.isoformat(), hhmm)
        for day, hhmm in zip(DAYS, times) if hhmm is not None
    }
    return TableHistory(name=name, finish_times=finish,
                        metrics_degraded=degraded)


ORDERS = history("orders", ["04:00", "04:10", "04:05", "05:30", "04:00"])
CUSTOMERS = history("customers", ["03:50", "03:45", "06:10", "03:50", "03:55"])


def test_the_worked_example_from_the_design_spec():
    result = recommend([ORDERS, CUSTOMERS], buffer_min=15)
    ordered = sorted(result.all_clear.values(), key=lambda m: m.time())
    assert [t.strftime("%H:%M") for t in ordered] == \
        ["04:00", "04:00", "04:10", "05:30", "06:10"]
    assert result.safe == time(6, 25)
    assert result.earlier == time(4, 25)
    assert result.n_days == 5


def test_all_clear_is_the_slowest_table_each_day_not_the_average():
    result = recommend([ORDERS, CUSTOMERS], buffer_min=0)
    assert result.all_clear[WED].strftime("%H:%M") == "06:10"   # customers late
    assert result.all_clear[THU].strftime("%H:%M") == "05:30"   # orders late


def test_a_day_counts_only_if_every_dependency_refreshed():
    patchy = history("fx_rates", ["02:15", None, "02:20", "02:10", None])
    result = recommend([ORDERS, patchy], buffer_min=15)
    assert result.n_days == 3, "days with a missing dependency must be excluded"
    assert set(result.all_clear) == {MON, WED, THU}


def test_missed_days_are_reported_not_silently_absorbed():
    """A daily table that skipped two days shows missed=2, and is not quietly
    relabelled as a table that only runs on the weekdays it managed.

    Deliberately 30 days, not 5. Over a five-day span a skipped Tuesday and a
    table that never runs on Tuesdays are the same observation, so the
    distinction this test is about cannot exist at that scale.
    """
    span = [date(2026, 8, 1) + timedelta(days=i) for i in range(30)]
    daily = TableHistory("orders", {d: f.at(d.isoformat(), "04:00") for d in span},
                         False)
    skipped_two = [d for i, d in enumerate(span) if i not in (7, 19)]
    patchy = TableHistory("fx_rates",
                          {d: f.at(d.isoformat(), "02:15") for d in skipped_two},
                          False)
    result = recommend([daily, patchy], buffer_min=15)
    row = next(r for r in result.rows if r.name == "fx_rates")
    assert row.refreshes == "daily"
    assert row.missed == 2
    assert result.n_days == 28, "the two incomplete days are excluded from timing"


def test_a_weekly_dependency_is_labelled_not_marked_as_missing():
    """silver.products refreshes Mondays. That is a row saying 'Mons', not
    four missed days."""
    weekly = history("products", ["06:20", None, None, None, None])
    result = recommend([ORDERS, weekly], buffer_min=15)
    row = next(r for r in result.rows if r.name == "products")
    assert row.refreshes == "Mons"
    assert row.missed == 0, "a weekly table has not missed the days it never runs"


# --- the weekday label --------------------------------------------------------

def test_mon_to_sat_is_not_daily():
    """Six of seven weekdays must not read as "daily".

    The old `len(seen) >= 6` fallback hid a Mon-Sat table. Its real schedule
    is the six weekdays it runs on.
    """
    span = [date(2026, 8, 1) + timedelta(days=i) for i in range(30)]
    days = [d for d in span if d.weekday() != 6]      # every day but Sunday
    assert _weekday_label(days, span, days) == "Mon,Tue,Wed,Thu,Fri,Sat"


def test_a_table_created_mid_window_is_still_daily():
    """A table born on day 10 of the window wrote every day of its own life,
    so it reads "daily". The coverage test uses the table's own span, not the
    whole window.
    """
    span = [date(2026, 8, 1) + timedelta(days=i) for i in range(30)]
    own = span[10:]
    assert _weekday_label(list(own), span, own) == "daily"


def test_gap_days_computes_the_cyclic_gap():
    assert max_gap_days(frozenset({0, 1, 2, 3, 4})) == 3      # Fri -> Mon
    assert max_gap_days(frozenset({0})) == 7                   # Mondays only
    assert max_gap_days(frozenset({0, 1, 2, 3, 4, 5})) == 2    # Sat -> Mon
    assert max_gap_days(frozenset(range(7))) == 1             # every day


def test_no_overlapping_days_yields_no_recommendation_rather_than_a_wrong_one():
    monday_only = history("a", ["04:00", None, None, None, None])
    friday_only = history("b", [None, None, None, None, "04:00"])
    result = recommend([monday_only, friday_only], buffer_min=15)
    assert result.safe is None and result.n_days == 0
    assert len(result.rows) == 2, "the evidence is still reported"


def test_single_day_of_history_makes_earlier_equal_safe():
    one = history("a", ["04:00", None, None, None, None])
    two = history("b", ["04:30", None, None, None, None])
    result = recommend([one, two], buffer_min=15)
    assert result.safe == time(4, 45) and result.earlier == time(4, 45)


def test_usual_is_the_median_and_worst_is_the_max():
    result = recommend([ORDERS, CUSTOMERS], buffer_min=15)
    row = next(r for r in result.rows if r.name == "orders")
    assert row.usual == time(4, 5) and row.worst == time(5, 30)


def test_degraded_flag_survives_into_the_row():
    flagged = history("adf_fed", ["04:00"] * 5, degraded=True)
    result = recommend([flagged], buffer_min=15)
    assert result.rows[0].degraded


# --- midnight rollover -------------------------------------------------------

def test_buffer_crossing_midnight_is_flagged_not_silently_wrapped():
    """A 23:50 all-clear plus a 15-minute buffer is 00:05 the NEXT day. The bare
    clock time reads as ~24h earlier, which is the dangerous direction: an
    operator scheduling 00:05 same-day runs before every dependency."""
    late = history("late", ["23:50"] * 5)
    result = recommend([late], buffer_min=15)
    assert result.safe == time(0, 5)
    assert result.safe_next_day, "the rollover must be reported, not discarded"


def test_a_normal_time_is_not_marked_next_day():
    result = recommend([ORDERS, CUSTOMERS], buffer_min=15)
    assert result.safe == time(6, 25)
    assert not result.safe_next_day and not result.earlier_next_day


def test_only_the_wrapping_number_is_marked():
    """safe wraps, earlier does not — the flags are independent."""
    a = history("a", ["23:50", "20:00", "20:00", "20:00", "20:00"])
    result = recommend([a], buffer_min=15)
    assert result.safe == time(0, 5) and result.safe_next_day
    assert result.earlier == time(20, 15) and not result.earlier_next_day


# --- outliers ----------------------------------------------------------------

OUTLIER_DAYS = [date(2026, 8, d) for d in range(1, 13)]


def outlier_history(name):
    """10 ordinary days at 04:00, 2 late days at 06:10."""
    finish = {
        d: f.at(d.isoformat(), "06:10" if d.day in (5, 9) else "04:00")
        for d in OUTLIER_DAYS
    }
    return TableHistory(name, finish)


def test_outliers_are_named_and_set_safe_apart_from_the_typical_time():
    result = recommend([outlier_history("c.s.x")], buffer_min=15)
    assert result.safe == time(6, 25), "safe still covers the late days"
    assert result.safe_without_outliers == time(4, 15)
    assert result.usual == time(4, 15)
    assert set(result.outlier_days) == {date(2026, 8, 5), date(2026, 8, 9)}


def test_two_outlier_days_move_earlier_to_the_typical_time():
    """The two late days are reruns, so they do not move `earlier`. It stays the
    typical day plus the buffer, 04:15, well before the 06:25 worst case.
    """
    result = recommend([outlier_history("c.s.x")], buffer_min=15)
    assert result.earlier == time(4, 15)
    assert result.earlier != result.safe


LOAD_DELAY_DAYS = [date(2026, 8, d) for d in range(1, 16)]


def test_earlier_ignores_load_delays_when_outliers_exist():
    """15 days: 11 at 04:00, 2 slow-load days at 04:20, 2 rerun days at 06:10.

    The slow-load days are 20 minutes late. They are NOT outliers. With the
    buffer the numbers are safe 06:25, safe_without_outliers 04:35, usual
    04:15. `earlier` must be the typical time 04:15, not the worst non-outlier
    day 04:35.
    """
    slow = {date(2026, 8, 6), date(2026, 8, 12)}
    late = {date(2026, 8, 5), date(2026, 8, 9)}
    finish = {
        d: f.at(d.isoformat(),
                "06:10" if d in late else "04:20" if d in slow else "04:00")
        for d in LOAD_DELAY_DAYS
    }
    result = recommend([TableHistory("c.s.x", finish)], buffer_min=15)
    assert set(result.outlier_days) == late, "a 20-minute load delay is not an outlier"
    assert result.safe == time(6, 25)
    assert result.safe_without_outliers == time(4, 35)
    assert result.usual == time(4, 15)
    assert result.earlier == time(4, 15)


def test_the_recommendation_field_names_are_stable():
    """The public shape of the result. New fields may be added; none may go."""
    from dataclasses import fields
    from view_scheduler.recommend import Recommendation
    names = [f.name for f in fields(Recommendation)]
    for name in ("safe", "safe_without_outliers", "usual", "earlier",
                 "outlier_days", "safe_next_day", "earlier_next_day",
                 "safe_without_outliers_next_day", "usual_next_day"):
        assert name in names


def test_a_clean_table_has_no_outliers():
    result = recommend([ORDERS, CUSTOMERS], buffer_min=15)
    assert result.outlier_days == ()
    assert result.safe_without_outliers == result.safe


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


def test_a_multi_daily_table_carries_its_slots_and_safe_slots():
    md = TableHistory("c.s.md", {d: f.at(d.isoformat(), "02:00") for d in DAYS},
                      multi_daily=True,
                      daily_slots=(time(2, 0), time(9, 0), time(17, 0)))
    row = recommend([md], buffer_min=15).rows[0]
    assert row.multi_daily and not row.continuous
    assert row.daily_slots == (time(2, 0), time(9, 0), time(17, 0))
    assert row.safe_slots == (time(2, 15), time(9, 15), time(17, 15))
