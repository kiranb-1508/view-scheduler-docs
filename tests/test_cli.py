"""The command line entry point and the packaging contract.

These guard the two things that break silently when a package is restructured:
the console script's argument handling, and the promise that the distribution
metadata matches what the code actually imports.
"""

import sys
from pathlib import Path

import pytest

from view_scheduler.cli import build_parser, main

REPO = Path(__file__).resolve().parent.parent

# tomllib is stdlib only from 3.11, but the package supports 3.10 — it does not
# read TOML itself, only these metadata tests do. Skip rather than narrowing
# requires-python for a test-only dependency.
tomllib = pytest.importorskip("tomllib") if sys.version_info >= (3, 11) else None
needs_tomllib = pytest.mark.skipif(
    sys.version_info < (3, 11), reason="tomllib is stdlib only from 3.11")


# --- argument handling -------------------------------------------------------

def test_the_three_config_fields_default_to_config_defaults():
    """The CLI must not invent its own defaults — they come from Config, so
    there is one place the numbers live."""
    from view_scheduler.config import Config
    args = build_parser().parse_args(["c.s.v"])
    assert args.days == Config.days_history
    assert args.buffer == Config.buffer_min
    assert args.timezone == Config.timezone


def test_config_flags_override():
    args = build_parser().parse_args(
        ["c.s.v", "--days", "60", "--buffer", "30", "--timezone", "UTC"])
    assert (args.days, args.buffer, args.timezone) == (60, 30, "UTC")


def test_warehouse_falls_back_to_the_environment(monkeypatch):
    monkeypatch.setenv("WAREHOUSE_ID", "from-env")
    # The default is bound at parser-build time, so build after setting the env.
    assert build_parser().parse_args(["c.s.v"]).warehouse_id == "from-env"


def test_an_explicit_warehouse_beats_the_environment(monkeypatch):
    monkeypatch.setenv("WAREHOUSE_ID", "from-env")
    args = build_parser().parse_args(["c.s.v", "--warehouse-id", "explicit"])
    assert args.warehouse_id == "explicit"


def test_missing_warehouse_fails_fast_rather_than_inside_the_first_query(
        monkeypatch, capsys):
    """Without this the run dies deep in an SDK call with an opaque message."""
    monkeypatch.delenv("WAREHOUSE_ID", raising=False)
    assert main(["c.s.v"]) == 2
    assert "no warehouse" in capsys.readouterr().err


def test_a_missing_view_argument_is_a_usage_error():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


# --- packaging contract ------------------------------------------------------

def pyproject() -> dict:
    with open(REPO / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)


@needs_tomllib
def test_the_console_script_points_at_something_importable():
    """A typo here produces a wheel whose command does not exist, and nothing
    catches it until someone installs it."""
    target = pyproject()["project"]["scripts"]["view-scheduler"]
    module, _, function = target.partition(":")
    imported = __import__(module, fromlist=[function])
    assert callable(getattr(imported, function))


@needs_tomllib
def test_the_runtime_dependency_matches_requirements_txt():
    """requirements.txt is what CI installs; pyproject is what users install.
    They drifting apart is how 'works in CI, broken on install' happens."""
    declared = pyproject()["project"]["dependencies"]
    assert any(d.startswith("databricks-sdk") for d in declared)
    text = (REPO / "requirements.txt").read_text()
    assert "databricks-sdk" in text


def test_the_package_still_exports_its_public_api():
    import view_scheduler
    assert set(view_scheduler.__all__) == {
        "analyze", "can_refresh", "Config", "GateResult", "Workspace"}
    for name in view_scheduler.__all__:
        assert getattr(view_scheduler, name) is not None
