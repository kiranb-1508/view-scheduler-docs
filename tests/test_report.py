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


# --- Tranche E: robustness warnings for unattended use -----------------------

def _hist(name, times, degraded=False, continuous=False):
    """times: {day: 'hh:mm'} for arbitrary days."""
    return TableHistory(
        name, {d: f.at(d.isoformat(), hhmm) for d, hhmm in times.items()},
        degraded, continuous)


def test_report_shows_a_days_column_of_per_dependency_coverage():
    dense = history("c.s.dense")                       # 10 days
    sparse = history("c.s.sparse", days=DAYS[:3])      # 3 days
    text = render("c.s.v", recommend([dense, sparse], 15), [], "Asia/Kolkata")
    assert "DAYS" in text
    dense_row = next(l for l in text.splitlines() if "c.s.dense" in l and "!" not in l)
    sparse_row = next(l for l in text.splitlines() if "c.s.sparse" in l and "!" not in l)
    # ... REFRESHES USUAL WORST DAYS MISSED — DAYS is the second-to-last field.
    assert dense_row.split()[-2] == "10"
    assert sparse_row.split()[-2] == "3"


def test_thin_evidence_warning_names_the_starving_dependency():
    dense = history("c.s.dense")
    sparse = history("c.s.sparse", days=DAYS[:3])
    text = render("c.s.v", recommend([dense, sparse], 15), [], "Asia/Kolkata")
    assert "weakly evidenced" in text
    assert "c.s.sparse has only 3 days" in text


def test_outlier_day_is_named_when_worst_is_far_above_usual():
    days = [date(2026, 8, d) for d in range(1, 16)]
    times = {d: "04:00" for d in days}
    times[days[6]] = "05:30"                            # one 90-min-late day
    result = recommend([_hist("c.s.orders", times)], 15)
    text = render("c.s.v", result, [], "Asia/Kolkata")
    assert "unusually late on 2026-08-07" in text
    assert "tighter applies" in text


def test_a_stable_table_is_not_flagged_as_an_outlier():
    days = [date(2026, 8, d) for d in range(1, 16)]
    # normal jitter, nothing anomalous
    times = {d: f"04:0{i % 6}" for i, d in enumerate(days)}
    result = recommend([_hist("c.s.steady", times)], 15)
    text = render("c.s.v", result, [], "Asia/Kolkata")
    assert "unusually late" not in text


def test_continuous_table_is_flagged_as_a_floor():
    result = recommend([_hist("c.s.stream", {DAYS[0]: "00:05"}, continuous=True)], 15)
    text = render("c.s.v", result, [], "Asia/Kolkata")
    assert "refreshes many times a day" in text and "floor" in text


def test_degraded_disclosure_says_misses_cannot_be_detected():
    result = recommend([_hist("c.s.adf", {d: "04:00" for d in DAYS}, degraded=True)], 15)
    text = render("c.s.v", result, [], "Asia/Kolkata")
    assert "without row metrics" in text and "skipped day cannot be detected" in text
