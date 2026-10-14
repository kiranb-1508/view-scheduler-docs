"""Phase 4 gate: warnings appear only when they apply. Plain words only."""

from datetime import date, time

from view_scheduler.models import Skipped, TableHistory
from view_scheduler.recommend import recommend
from view_scheduler.report import render
from tests import fixtures as f

DAYS = [date(2026, 8, d) for d in range(1, 11)]


def history(name, hhmm="04:00", days=DAYS, degraded=False):
    return TableHistory(name, {d: f.at(d.isoformat(), hhmm) for d in days},
                        degraded)


def test_clean_run_prints_no_warning_section():
    result = recommend([history("c.s.a"), history("c.s.b", "03:30")], 15)
    text = render("c.s.v", result, [], "Asia/Kolkata")
    assert "!" not in text
    assert "SCHEDULE AT  04:15" in text


def test_skipped_dependency_surfaces_with_its_reason():
    result = recommend([history("c.s.a")], 15)
    text = render("c.s.v", result, [Skipped("ext.crm", "not readable as a Delta table")],
                  "Asia/Kolkata")
    assert "1 dependency could not be read" in text
    assert "ext.crm — not readable as a Delta table" in text


def test_weekly_dependency_gets_a_line():
    weekly = history("c.s.products", "06:20", days=[DAYS[0], DAYS[7]])
    result = recommend([history("c.s.a"), weekly], 15)
    text = render("c.s.v", result, [], "Asia/Kolkata")
    assert "c.s.products" in text and "Sats" in text
    assert "runs only on Sats" in text


def test_degraded_table_gets_a_line():
    result = recommend([history("c.s.adf", degraded=True)], 15)
    text = render("c.s.v", result, [], "Asia/Kolkata")
    assert "writes no row counts" in text


def test_no_overlapping_days_says_so_instead_of_inventing_a_time():
    a = TableHistory("c.s.a", {DAYS[0]: f.at(DAYS[0].isoformat(), "04:00")}, False)
    b = TableHistory("c.s.b", {DAYS[5]: f.at(DAYS[5].isoformat(), "04:00")}, False)
    text = render("c.s.v", recommend([a, b], 15), [], "Asia/Kolkata")
    assert "NO TIME TO SUGGEST" in text
    assert "SCHEDULE AT" not in text


def test_counts_read_grammatically_at_one_and_many():
    from view_scheduler.report import _plural
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
    # ... DEPENDENCY RUNS USUAL LATEST DAYS MISSED — DAYS is the second-to-last.
    assert dense_row.split()[-2] == "10"
    assert sparse_row.split()[-2] == "3"


def test_thin_evidence_warning_names_the_starving_dependency():
    dense = history("c.s.dense")
    sparse = history("c.s.sparse", days=DAYS[:3])
    text = render("c.s.v", recommend([dense, sparse], 15), [], "Asia/Kolkata")
    assert "this advice is weak" in text
    assert "c.s.sparse ran on only 3 days" in text


def test_outlier_day_is_named_when_worst_is_far_above_usual():
    days = [date(2026, 8, d) for d in range(1, 16)]
    times = {d: "04:00" for d in days}
    times[days[6]] = "05:30"                            # one 90-min-late day
    result = recommend([_hist("c.s.orders", times)], 15)
    text = render("c.s.v", result, [], "Asia/Kolkata")
    assert "finished very late on 2026-08-07" in text
    assert "Without it you could use" in text


def test_a_stable_table_is_not_flagged_as_an_outlier():
    days = [date(2026, 8, d) for d in range(1, 16)]
    times = {d: f"04:0{i % 6}" for i, d in enumerate(days)}
    result = recommend([_hist("c.s.steady", times)], 15)
    text = render("c.s.v", result, [], "Asia/Kolkata")
    assert "finished very late" not in text


def test_continuous_table_is_flagged_as_a_floor():
    result = recommend([_hist("c.s.stream", {DAYS[0]: "00:05"}, continuous=True)], 15)
    text = render("c.s.v", result, [], "Asia/Kolkata")
    assert "runs many times a day" in text and "fully caught up" in text


def test_degraded_disclosure_says_misses_cannot_be_detected():
    result = recommend([_hist("c.s.adf", {d: "04:00" for d in DAYS}, degraded=True)], 15)
    text = render("c.s.v", result, [], "Asia/Kolkata")
    assert "writes no row counts" in text and "missed day cannot be spotted" in text


# --- midnight rollover -------------------------------------------------------

def test_a_start_time_past_midnight_is_marked_and_warned():
    """23:50 + 15min gap = 00:05 next day. Printing a bare 00:05 invites the
    operator to schedule it same-day, which runs before every input."""
    result = recommend([history("c.s.a", "23:50")], 15)
    text = render("c.s.v", result, [], "Asia/Kolkata")
    assert "00:05 (+1 day)" in text, "the bare clock time is the dangerous reading"
    assert "is on the NEXT day" in text
    assert "yesterday's data" in text


def test_an_ordinary_time_carries_no_day_marker():
    result = recommend([history("c.s.a", "04:00")], 15)
    text = render("c.s.v", result, [], "Asia/Kolkata")
    assert "(+1 day)" not in text
    assert "NEXT day" not in text


# --- unrecognised operations -------------------------------------------------

def test_unknown_operations_are_named_in_the_report():
    h = TableHistory("c.s.a", {d: f.at(d.isoformat(), "04:00") for d in DAYS},
                     unknown_ops=("CLUSTER BY", "REORG"))
    result = recommend([h], 15)
    text = render("c.s.v", result, [], "Asia/Kolkata")
    assert "did not recognise" in text
    assert "CLUSTER BY, REORG" in text


def test_no_unknown_warning_when_everything_classified():
    result = recommend([history("c.s.a")], 15)
    assert "did not recognise" not in render("c.s.v", result, [], "Asia/Kolkata")


# --- the day-by-day evidence -------------------------------------------------

def test_the_day_by_day_series_is_rendered():
    """The days are on screen, not just a median and a max. Without this the
    reader cannot check which day set which time."""
    result = recommend([history("c.s.a", "04:00"), history("c.s.b", "03:30")], 15)
    text = render("c.s.v", result, [], "Asia/Kolkata")
    assert "WHEN THE LAST JOB FINISHED" in text
    for day in DAYS:
        assert day.isoformat() in text, f"{day} missing from the evidence"


def test_the_series_names_which_days_set_the_two_numbers():
    a = TableHistory("c.s.a", {
        DAYS[0]: f.at(DAYS[0].isoformat(), "06:10"),   # latest
        DAYS[1]: f.at(DAYS[1].isoformat(), "05:30"),   # second latest
        DAYS[2]: f.at(DAYS[2].isoformat(), "04:00"),
    })
    text = render("c.s.v", recommend([a], 15), [], "Asia/Kolkata")
    lines = [ln for ln in text.splitlines()
             if "sets the safe time" in ln or "sets the earlier time" in ln]
    assert len(lines) == 2
    assert DAYS[0].isoformat() in lines[0] and "06:10" in lines[0]
    assert DAYS[1].isoformat() in lines[1] and "05:30" in lines[1]


def test_no_series_when_there_is_no_overlapping_day():
    a = TableHistory("c.s.a", {DAYS[0]: f.at(DAYS[0].isoformat(), "04:00")})
    b = TableHistory("c.s.b", {DAYS[1]: f.at(DAYS[1].isoformat(), "04:00")})
    text = render("c.s.v", recommend([a, b], 15), [], "Asia/Kolkata")
    assert "WHEN THE LAST JOB FINISHED" not in text


# --- outliers ----------------------------------------------------------------

def _outlier_table(name="c.s.x"):
    days = [date(2026, 8, d) for d in range(1, 13)]
    return TableHistory(name, {
        d: f.at(d.isoformat(), "06:10" if d.day in (5, 9) else "04:00")
        for d in days
    })


def test_outliers_get_a_without_outliers_line():
    text = render("c.s.v", recommend([_outlier_table()], 15), [], "Asia/Kolkata")
    assert "WITHOUT OUTLIERS  04:15" in text
    assert "excludes 2 outlier days" in text


def test_a_clean_table_gets_no_without_outliers_line():
    result = recommend([history("c.s.a")], 15)
    assert "WITHOUT OUTLIERS" not in render("c.s.v", result, [], "Asia/Kolkata")


# --- multi-daily tables ------------------------------------------------------

def test_multi_daily_table_gets_a_slot_warning():
    md = TableHistory("c.s.md", {d: f.at(d.isoformat(), "02:00") for d in DAYS},
                      multi_daily=True,
                      daily_slots=(time(2, 0), time(9, 0), time(17, 0)))
    text = render("c.s.v", recommend([md], 15), [], "Asia/Kolkata")
    assert "refreshes 3 times a day" in text
    assert "usually 02:00, 09:00, 17:00" in text
    assert "safe at 02:15, 09:15, 17:15" in text


def test_a_once_a_day_table_gets_no_slot_warning():
    result = recommend([history("c.s.a")], 15)
    assert "times a day" not in render("c.s.v", result, [], "Asia/Kolkata")
