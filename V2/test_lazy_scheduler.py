from datetime import date, datetime, timedelta

from V2.lazy_scheduler import (
    Config,
    TableRef,
    classify,
    finish_times,
    render,
    walk,
)
from V2.lazy_scheduler import _table_analysis, recommend


def history_row(day, hour, operation="WRITE", mode="Overwrite", metrics=None):
    return {
        "timestamp": datetime(day.year, day.month, day.day, hour),
        "operation": operation,
        "operationParameters": {"mode": mode} if mode is not None else {},
        "operationMetrics": metrics if metrics is not None else {"numOutputRows": "1"},
    }


def test_classification_uses_write_mode_and_flags_degraded_metrics():
    assert classify(history_row(date(2026, 9, 1), 7, mode="Overwrite")).qualifies
    assert classify(history_row(date(2026, 9, 1), 7, mode="Append", metrics={})).flag == (
        "metrics-degraded"
    )
    assert classify(history_row(date(2026, 9, 1), 7, mode="Append", metrics={"numOutputRows": "0"})).status == "ran-empty"
    assert classify(history_row(date(2026, 9, 1), 7, mode=None)).flag == "unclassified-write"
    assert not classify(history_row(date(2026, 9, 1), 7, operation="OPTIMIZE", mode=None)).qualifies


def test_walk_handles_nested_views_diamonds_cycles_and_functions():
    metadata = {
        "root": {"table_type": "VIEW", "view_dependencies": {"dependencies": [{"table_full_name": "a"}, {"table_full_name": "b"}, {"function": "f"}]}},
        "a": {"table_type": "VIEW", "view_dependencies": {"dependencies": [{"table_full_name": "leaf"}]}},
        "b": {"table_type": "VIEW", "view_dependencies": {"dependencies": [{"table_full_name": "leaf"}]}},
        "leaf": {"table_type": "EXTERNAL"},
    }
    calls = []

    def fetch(name):
        calls.append(name)
        return metadata[name]

    result = walk("root", fetch)
    assert [ref.name for ref in result.batch] == ["leaf"]
    assert calls.count("leaf") == 1
    assert any(skip.reason == "function dependency" for skip in result.skipped)

    cycle_metadata = {
        "a": {"table_type": "VIEW", "view_dependencies": {"dependencies": [{"table_full_name": "b"}]}},
        "b": {"table_type": "VIEW", "view_dependencies": {"dependencies": [{"table_full_name": "a"}]}},
    }
    cycle = walk("a", cycle_metadata.get)
    assert any(skip.reason == "cycle" for skip in cycle.skipped)


def test_finish_times_uses_local_clock_and_first_qualifying_commit():
    config = Config(timezone="Asia/Kolkata")
    today = date(2026, 9, 3)
    rows = [
        {**history_row(date(2026, 9, 3), 4), "timestamp": datetime(2026, 9, 2, 22, 45)},
        history_row(today, 8),
        history_row(today, 7),
    ]
    summary = finish_times(rows, today, config)
    assert summary.finish_times[today] == 7 * 60
    assert summary.oldest_entry == date(2026, 9, 2)


def test_recommendation_excludes_one_outlier_from_sooner_time():
    config = Config(days_history=5, buffer_min=15)
    start = date(2026, 9, 1)
    rows = [
        history_row(start + timedelta(days=index), 7 if index < 4 else 8)
        for index in range(5)
    ]
    summary = finish_times(rows, start + timedelta(days=4), config)
    table = _table_analysis(TableRef("orders", "EXTERNAL"), summary, start + timedelta(days=4), config)
    recommendation = recommend([table], start + timedelta(days=4), config)
    assert recommendation.safe_min == 8 * 60 + 15
    assert recommendation.sooner_min == 7 * 60 + 15
    assert recommendation.early_days == 1
    assert recommendation.worst_gap_min == 60


def test_never_refreshed_table_blocks_recommendation():
    config = Config(days_history=3)
    today = date(2026, 9, 3)
    summary = finish_times(
        [
            history_row(today - timedelta(days=offset), 7, operation="OPTIMIZE", mode=None)
            for offset in range(3)
        ],
        today,
        config,
    )
    table = _table_analysis(TableRef("orders", "EXTERNAL"), summary, today, config)
    recommendation = recommend([table], today, config)
    assert recommendation.safe_min is None
    assert len(recommendation.blocked) == 3


def test_render_surfaces_skipped_dependencies():
    config = Config()
    today = date(2026, 9, 3)
    summary = finish_times([history_row(today, 7)], today, config)
    table = _table_analysis(TableRef("orders", "EXTERNAL"), summary, today, config)
    recommendation = recommend([table], today, config)
    from V2.lazy_scheduler import Report, Skipped

    output = render(
        Report(
            "catalog.schema.view",
            config,
            today,
            recommendation,
            [table],
            [Skipped("crm", "403")],
        ),
        today,
    )
    assert "NOT CHECKED" in output
    assert "crm: 403" in output
