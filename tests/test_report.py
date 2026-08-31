"""Phase 4 gate: warnings appear only when they apply."""

from datetime import date, time

from scheduler.models import Skipped, TableHistory
from scheduler.recommend import recommend
from scheduler.report import render
from tests import fixtures as f

DAYS = [date(2026, 8, d) for d in range(1, 11)]


def history(name, hhmm="04:00", days=DAYS, degraded=False):
    return TableHistory(name, {d: f.at(d.isoformat(), hhmm) for d in days},
                        degraded)


def test_clean_run_prints_no_warning_section():
    result = recommend([history("c.s.a"), history("c.s.b", "03:30")], 15)
    text = render("c.s.v", result, [], "Asia/Kolkata")
    assert "!" not in text
    assert "START AT 04:15" in text


def test_skipped_dependency_surfaces_with_its_reason():
    result = recommend([history("c.s.a")], 15)
    text = render("c.s.v", result, [Skipped("ext.crm", "not readable as a Delta table")],
                  "Asia/Kolkata")
    assert "1 dependency skipped" in text
    assert "ext.crm — not readable as a Delta table" in text


def test_weekly_dependency_gets_a_line():
    weekly = history("c.s.products", "06:20", days=[DAYS[0], DAYS[7]])
    result = recommend([history("c.s.a"), weekly], 15)
    text = render("c.s.v", result, [], "Asia/Kolkata")
    assert "c.s.products  Sats" in text
    assert "refreshes Sats only" in text


def test_degraded_table_gets_a_line():
    result = recommend([history("c.s.adf", degraded=True)], 15)
    text = render("c.s.v", result, [], "Asia/Kolkata")
    assert "without row metrics" in text


def test_no_overlapping_days_says_so_instead_of_inventing_a_time():
    a = TableHistory("c.s.a", {DAYS[0]: f.at(DAYS[0].isoformat(), "04:00")}, False)
    b = TableHistory("c.s.b", {DAYS[5]: f.at(DAYS[5].isoformat(), "04:00")}, False)
    text = render("c.s.v", recommend([a, b], 15), [], "Asia/Kolkata")
    assert "NO RECOMMENDATION" in text
    assert "START AT" not in text


def test_counts_read_grammatically_at_one_and_many():
    from scheduler.report import _plural
    assert _plural(1, "dependency") == "1 dependency"
    assert _plural(2, "dependency") == "2 dependencies"
    assert _plural(1, "day") == "1 day"
    assert _plural(30, "day") == "30 days"
