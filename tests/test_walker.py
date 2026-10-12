"""Phase 1 gate: nested views, a diamond, a cycle, FOREIGN, a function
dependency, and a 403 that must not abort the walk."""

import pytest

from view_scheduler.walker import walk
from tests import fixtures as f


def fetcher(catalog: dict, fail: dict[str, Exception] | None = None):
    """Fetch from a dict, counting calls, optionally raising for some names."""
    fail = fail or {}
    calls: list[str] = []

    def fetch(name: str) -> dict:
        calls.append(name)
        if name in fail:
            raise fail[name]
        if name not in catalog:
            raise KeyError(f"Table '{name}' does not exist")
        return catalog[name]

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def table_names(result):
    return [table.name for table in result.tables]


def test_nested_views_resolve_to_base_tables():
    result = walk("c.s.v_top", fetcher(f.DIAMOND))
    assert sorted(table_names(result)) == ["c.s.base_a", "c.s.base_b"]
    assert not result.blockers and not result.notes


def test_diamond_visits_shared_dependency_exactly_once():
    fetch = fetcher(f.DIAMOND)
    result = walk("c.s.v_top", fetch)
    assert fetch.calls.count("c.s.base_a") == 1, "memoization failed"
    assert table_names(result).count("c.s.base_a") == 1, "duplicate gating table"


def test_cycle_terminates():
    result = walk("c.s.v_a", fetcher(f.CYCLE))
    assert result.tables == ()
    assert len(result.blockers) <= 1


def test_foreign_table_is_skipped_with_a_reason():
    catalog = {
        "c.s.v": f.uc_view("c.s.v", ["c.s.t", "c.s.ext"]),
        "c.s.t": f.uc_table("c.s.t"),
        "c.s.ext": f.uc_table("c.s.ext", table_type="FOREIGN"),
    }
    result = walk("c.s.v", fetcher(catalog))
    assert table_names(result) == ["c.s.t"]
    assert [s.name for s in result.blockers] == ["c.s.ext"]
    assert "FOREIGN" in result.blockers[0].reason


def test_function_dependency_is_skipped_not_treated_as_a_table():
    catalog = {
        "c.s.v": f.uc_view("c.s.v", ["c.s.t"], functions=1),
        "c.s.t": f.uc_table("c.s.t"),
    }
    result = walk("c.s.v", fetcher(catalog))
    assert table_names(result) == ["c.s.t"], "a real dependency was lost to the function branch"
    # A function is a note, never a blocker: there is no refresh to wait for.
    assert len(result.notes) == 1 and "function" in result.notes[0].reason
    assert not result.blockers


def test_permission_error_on_one_node_does_not_abort_the_walk():
    catalog = dict(f.DIAMOND)
    fetch = fetcher(catalog, fail={"c.s.base_b": PermissionError(
        "User does not have SELECT on c.s.base_b")})
    result = walk("c.s.v_top", fetch)
    assert table_names(result) == ["c.s.base_a"], \
        "the readable branch must still be returned"
    assert [(s.name, s.reason) for s in result.blockers] == [
        ("c.s.base_b", "permission denied")]


def test_depth_cap_is_enforced():
    chain = {f"c.s.v{i}": f.uc_view(f"c.s.v{i}", [f"c.s.v{i+1}"]) for i in range(10)}
    chain["c.s.v10"] = f.uc_table("c.s.v10")
    result = walk("c.s.v0", fetcher(chain), max_depth=4)
    assert result.tables == ()
    assert any("depth cap" in s.reason for s in result.blockers)


def test_streaming_table_and_materialized_view_gate():
    catalog = {
        "c.s.v": f.uc_view("c.s.v", ["c.s.st", "c.s.mv"]),
        "c.s.st": f.uc_table("c.s.st", table_type="STREAMING_TABLE"),
        "c.s.mv": f.uc_table("c.s.mv", table_type="MATERIALIZED_VIEW"),
    }
    result = walk("c.s.v", fetcher(catalog))
    assert sorted(table_names(result)) == ["c.s.mv", "c.s.st"]
    assert not result.blockers and not result.notes
