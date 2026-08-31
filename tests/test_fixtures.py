"""Phase 0 gate: the fixtures are the shapes we claim they are."""

import json

from tests import fixtures as f


def test_view_dependency_nests_name_under_table_key():
    dep = f.DIAMOND["c.s.v_top"]["view_dependencies"]["dependencies"][0]
    assert "table_full_name" not in dep, "flat shape is the docs' bug, not the API's"
    assert dep["table"]["table_full_name"] == "c.s.base_b"


def test_function_dependency_has_no_table_key():
    view = f.uc_view("c.s.v", ["c.s.t"], functions=1)
    deps = view["view_dependencies"]["dependencies"]
    assert "table" not in deps[-1] and "function" in deps[-1]


def test_history_nested_fields_are_json_strings():
    row = f.OPERATION_ZOO[3]
    assert isinstance(row["operationParameters"], str)
    assert json.loads(row["operationParameters"])["mode"] == "Append"


def test_absent_metrics_is_none_not_empty_dict():
    update = next(r for r in f.OPERATION_ZOO if r["operation"] == "UPDATE")
    ctas = next(r for r in f.OPERATION_ZOO if r["operation"] == "CREATE OR REPLACE TABLE")
    assert update["operationMetrics"] is None
    assert json.loads(ctas["operationMetrics"]) == {}


def test_daily_history_one_commit_per_day():
    rows = f.daily_history([("2026-08-01", "04:00"), ("2026-08-02", "04:10")])
    assert len(rows) == 2
    assert {r["timestamp"][:10] for r in rows} == {"2026-08-01", "2026-08-02"}
