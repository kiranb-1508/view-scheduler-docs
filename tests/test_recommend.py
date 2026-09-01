"""Phase 3 gate. The anchor is the worked example the design spec states
outright: safe 06:25, tighter 05:45, buffer 15."""

from datetime import date, time, timedelta
from zoneinfo import ZoneInfo

from scheduler.models import TableHistory
from scheduler.recommend import recommend
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
    assert result.tighter == time(5, 45)
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


def test_no_overlapping_days_yields_no_recommendation_rather_than_a_wrong_one():
    monday_only = history("a", ["04:00", None, None, None, None])
    friday_only = history("b", [None, None, None, None, "04:00"])
    result = recommend([monday_only, friday_only], buffer_min=15)
    assert result.safe is None and result.n_days == 0
    assert len(result.rows) == 2, "the evidence is still reported"


def test_single_day_of_history_makes_tighter_equal_safe():
    one = history("a", ["04:00", None, None, None, None])
    two = history("b", ["04:30", None, None, None, None])
    result = recommend([one, two], buffer_min=15)
    assert result.safe == time(4, 45) and result.tighter == time(4, 45)


def test_usual_is_the_median_and_worst_is_the_max():
    result = recommend([ORDERS, CUSTOMERS], buffer_min=15)
    row = next(r for r in result.rows if r.name == "orders")
    assert row.usual == time(4, 5) and row.worst == time(5, 30)


def test_degraded_flag_survives_into_the_row():
    flagged = history("adf_fed", ["04:00"] * 5, degraded=True)
    result = recommend([flagged], buffer_min=15)
    assert result.rows[0].degraded
