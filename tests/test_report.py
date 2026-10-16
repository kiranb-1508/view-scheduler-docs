"""Phase 4 gate: warnings appear only when they apply. Plain words only."""

from dataclasses import replace
from datetime import date, datetime, time, timedelta

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


def test_unreadable_dependency_gets_its_own_no_time_wording():
    """run.py withholds the times when a dependency cannot be read. The report
    must say why, and still print the evidence it could read. The wording stays
    distinct from the no-common-day case."""
    result = recommend([history("c.s.a"), history("c.s.b", "03:30")], 15)
    blocked = replace(result, safe=None, earlier=None, usual=None,
                      safe_without_outliers=None, safe_next_day=False,
                      earlier_next_day=False,
                      safe_without_outliers_next_day=False, usual_next_day=False)
    text = render("c.s.v", blocked, [Skipped(
        "c.s.secret", "permission denied — cannot read its history")],
        "Asia/Kolkata")
    assert "NO TIME TO SUGGEST — 1 dependency could not be read." in text
    assert "The times below cover only the 2 dependencies that were read." in text
    assert "there is no day where every job had finished" not in text
    assert "WHEN THE LAST JOB FINISHED" in text   # readable evidence still prints
    assert "2026-08-01" in text                   # ... the rows, not just a header
    assert "sets the" not in text                 # no mark for an unprinted time


def test_the_no_common_day_wording_is_not_the_unreadable_wording():
    a = TableHistory("c.s.a", {DAYS[0]: f.at(DAYS[0].isoformat(), "04:00")}, False)
    b = TableHistory("c.s.b", {DAYS[5]: f.at(DAYS[5].isoformat(), "04:00")}, False)
    text = render("c.s.v", recommend([a, b], 15), [], "Asia/Kolkata")
    assert "there is no day where every job had finished" in text
    assert "could not be read" not in text


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


def test_the_outlier_warning_names_the_with_outliers_time():
    """The late day sets the WITH OUTLIERS time, not SCHEDULE AT. SCHEDULE AT
    shows safe_without_outliers, which is a different number."""
    result = recommend([_outlier_table()], 15)
    text = render("c.s.v", result, [], "Asia/Kolkata")
    assert "sets the WITH OUTLIERS time" in text
    assert "sets the safe time" not in text
    # Two days stand out here, so the advice is plural.
    assert "Without them you could use 04:15" in text


def test_the_outlier_warning_says_one_day_when_only_one_stands_out():
    days = [date(2026, 8, d) for d in range(1, 16)]
    times = {d: "04:00" for d in days}
    times[days[6]] = "05:30"
    result = recommend([_hist("c.s.orders", times)], 15)
    assert len(result.outlier_days) == 1
    text = render("c.s.v", result, [], "Asia/Kolkata")
    assert "that one day sets the WITH OUTLIERS time" in text


def test_the_outlier_warning_fits_a_count_of_two_or_more():
    """Two days stand out, but only one row pins `worst`. The wording must not
    call a pair of days 'that one day'."""
    result = recommend([_outlier_table()], 15)
    assert len(result.outlier_days) == 2
    text = render("c.s.v", result, [], "Asia/Kolkata")
    assert "that one day" not in text
    assert "2 outlier days set the WITH OUTLIERS time" in text


def test_the_outlier_advice_keeps_the_singular_pronoun_for_one_day():
    """One day stands out, so the closing advice stays singular."""
    days = [date(2026, 8, d) for d in range(1, 16)]
    times = {d: "04:00" for d in days}
    times[days[6]] = "05:30"
    result = recommend([_hist("c.s.orders", times)], 15)
    assert len(result.outlier_days) == 1
    text = render("c.s.v", result, [], "Asia/Kolkata")
    assert "Without it you could use" in text
    assert "Without them" not in text


def test_the_outlier_advice_uses_the_plural_pronoun_for_two_or_more_days():
    """Two days set the WITH OUTLIERS time, so 'it' is wrong. The advice must
    read 'Without them' and still name the earlier time."""
    result = recommend([_outlier_table()], 15)
    assert len(result.outlier_days) == 2
    text = render("c.s.v", result, [], "Asia/Kolkata")
    assert "Without them you could use 04:15" in text
    assert "Without it you could use" not in text


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


def test_the_series_names_the_day_that_sets_the_printed_time():
    """One mark, on the day behind SCHEDULE AT. No day sets `earlier`: it is
    the median of the window. The mark shows the raw finish, so the printed
    number must be that raw time plus the buffer."""
    a = TableHistory("c.s.a", {
        DAYS[0]: f.at(DAYS[0].isoformat(), "06:10"),   # latest, sets the time
        DAYS[1]: f.at(DAYS[1].isoformat(), "05:30"),
        DAYS[2]: f.at(DAYS[2].isoformat(), "04:00"),
    })
    result = recommend([a], 15)
    text = render("c.s.v", result, [], "Asia/Kolkata")
    marks = [ln for ln in text.splitlines() if "←" in ln]
    assert not [ln for ln in marks if "sets the earlier time" in ln], \
        "earlier is a median, so no single day sets it"
    assert len(marks) == 1, marks
    assert "sets the SCHEDULE AT time" in marks[0]
    assert date.fromisoformat(marks[0].split()[0]) == DAYS[0]
    raw = time.fromisoformat(marks[0].split()[2])
    assert (datetime.combine(date(2000, 1, 1), raw)
            + timedelta(minutes=15)).time() == result.safe


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


def test_the_headline_puts_the_outlier_free_time_first():
    """With outlier days, SCHEDULE AT is the time that holds on the normal
    days. WITH OUTLIERS then covers every day. The old layout was inverted and
    printed the same clock time twice."""
    days = [date(2026, 8, d) for d in range(1, 16)]
    slow = {date(2026, 8, 6), date(2026, 8, 12)}      # 20 minutes late, normal
    late = {date(2026, 8, 5), date(2026, 8, 9)}       # reruns, outliers
    times = {d: ("06:10" if d in late else "04:20" if d in slow else "04:00")
             for d in days}
    text = render("c.s.v", recommend([_hist("c.s.x", times)], 15), [],
                  "Asia/Kolkata")
    lines = text.splitlines()
    assert ("       SCHEDULE AT  04:35    safe on 13 of 15 days — "
            "excludes 2 outlier days") in lines
    assert "       WITH OUTLIERS  06:25    safe on all 15 days" in lines
    assert any(line.startswith("       EARLIER      04:15    ") for line in lines)


def test_the_headline_keeps_its_old_shape_without_outliers():
    result = recommend([history("c.s.a")], 15)
    text = render("c.s.v", result, [], "Asia/Kolkata")
    assert "SCHEDULE AT  04:15" in text
    assert "safe — every job had finished, on all 10 days" in text
    assert "WITH OUTLIERS" not in text


def test_outliers_get_a_with_outliers_line():
    text = render("c.s.v", recommend([_outlier_table()], 15), [], "Asia/Kolkata")
    assert "SCHEDULE AT  04:15" in text
    assert "WITH OUTLIERS  06:25" in text
    assert "excludes 2 outlier days" in text


def test_a_clean_table_gets_no_with_outliers_line():
    result = recommend([history("c.s.a")], 15)
    assert "WITH OUTLIERS" not in render("c.s.v", result, [], "Asia/Kolkata")


def test_the_evidence_marks_name_the_day_that_sets_each_printed_time():
    """Days 5 and 9 are the outliers, so they are the two latest days. With the
    new rule `earlier` is the median of the normal days, so no single day sets
    it. The mark must not sit on an outlier day."""
    result = recommend([_outlier_table()], 15)
    evidence = [ln for ln in render("c.s.v", result, [], "Asia/Kolkata").splitlines()
                if "←" in ln]
    earlier_marks = [ln for ln in evidence if "sets the earlier time" in ln]
    assert not earlier_marks, "earlier is a median now, so no one day sets it"

    schedule = next(ln for ln in evidence if "sets the SCHEDULE AT time" in ln)
    schedule_day = date.fromisoformat(schedule.split()[0])
    assert schedule_day not in result.outlier_days
    # The line shows the raw finish. The printed number is that time plus the
    # buffer, so this day really produces the printed SCHEDULE AT value.
    raw = time.fromisoformat(schedule.split()[2])
    assert (datetime.combine(date(2000, 1, 1), raw)
            + timedelta(minutes=15)).time() == result.safe_without_outliers

    worst = next(ln for ln in evidence if "sets the WITH OUTLIERS time" in ln)
    assert date.fromisoformat(worst.split()[0]) in result.outlier_days
    worst_raw = time.fromisoformat(worst.split()[2])
    assert (datetime.combine(date(2000, 1, 1), worst_raw)
            + timedelta(minutes=15)).time() == result.safe


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
