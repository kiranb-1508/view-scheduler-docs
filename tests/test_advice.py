"""The easy front door: advise() prints a plain report and returns an Advice."""

import json
from datetime import date, time

import pytest

from conftest import FakeWorkspace
from tests import fixtures as f
from view_scheduler.advice import Advice, DependencyRun, advise
from view_scheduler.config import Config

CONFIG = Config(timezone="UTC")
TODAY = date(2026, 8, 6)


def commit(day, hhmm):
    return f.raw_history_row(0, f.at(day, hhmm), "WRITE",
                             {"mode": "Overwrite"}, {"numOutputRows": "10"})


def workspace(times, errors=None):
    """times: {'c.s.t1': {'2026-08-04': '03:00', ...}, ...}"""
    tables = {"c.s.v": f.uc_view("c.s.v", list(times)),
              **{name: f.uc_table(name) for name in times}}
    history = {name: [commit(day, hhmm) for day, hhmm in day_times.items()]
               for name, day_times in times.items()}
    return FakeWorkspace(tables=tables, history=history, history_errors=errors)


BUSY = {
    "c.s.t1": {"2026-08-04": "03:00", "2026-08-05": "03:00", "2026-08-06": "03:00"},
    "c.s.t2": {"2026-08-04": "04:00", "2026-08-05": "05:00", "2026-08-06": "06:00"},
}


def test_advise_returns_the_two_times_and_the_rows():
    advice = advise("c.s.v", workspace(BUSY), CONFIG, today=TODAY, quiet=True)
    assert advice.safe_time == time(6, 15)
    assert advice.earlier_time == time(5, 15)
    assert advice.next_day is False
    assert advice.days_used == 3
    assert [row.name for row in advice.dependencies] == ["c.s.t1", "c.s.t2"]
    assert advice.dependencies[1].runs == "daily"
    assert advice.dependencies[1].usual == time(5, 0)
    assert advice.dependencies[1].latest == time(6, 0)


def test_advise_prints_by_default_and_is_silent_when_quiet(capsys):
    advise("c.s.v", workspace(BUSY), CONFIG, today=TODAY)
    out = capsys.readouterr().out
    assert "SCHEDULE AT  06:15" in out
    assert "WHEN THE LAST JOB FINISHED" in out

    advise("c.s.v", workspace(BUSY), CONFIG, today=TODAY, quiet=True)
    assert capsys.readouterr().out == ""


def test_the_text_matches_render():
    advice = advise("c.s.v", workspace(BUSY), CONFIG, today=TODAY, quiet=True)
    assert str(advice) == advice.render()
    assert "VIEW   c.s.v" in advice.render()


def test_the_report_uses_no_jargon():
    """The words that non-specialists stumble on must be gone."""
    text = advise("c.s.v", workspace(BUSY), CONFIG, today=TODAY, quiet=True).render()
    for word in ("tighter", "all-clear", "ALL-CLEAR", "advisory"):
        assert word not in text


def test_to_dict_is_json_serialisable():
    advice = advise("c.s.v", workspace(BUSY), CONFIG, today=TODAY, quiet=True)
    payload = advice.to_dict()
    assert payload["safe_time"] == "06:15"
    assert payload["earlier_time"] == "05:15"
    assert payload["dependencies"][0]["name"] == "c.s.t1"
    assert len(payload["finished_by_day"]) == 3
    json.dumps(payload)   # must not raise


def test_no_overlap_gives_no_safe_time():
    times = {"c.s.t1": {"2026-08-04": "03:00"},
             "c.s.t2": {"2026-08-05": "03:00"}}
    advice = advise("c.s.v", workspace(times), CONFIG, today=TODAY, quiet=True)
    assert advice.safe_time is None
    assert advice.earlier_time is None
    assert "NO TIME TO SUGGEST" in advice.render()


def test_unreadable_dependency_is_carried_and_reported():
    ws = workspace(BUSY, errors={"c.s.t2": RuntimeError(
        "User does not have SELECT on c.s.t2")})
    advice = advise("c.s.v", ws, CONFIG, today=TODAY, quiet=True)
    assert [item.name for item in advice.unreadable] == ["c.s.t2"]
    assert "could not be read" in advice.render()
    assert advice.to_dict()["unreadable"][0]["name"] == "c.s.t2"


def test_advice_is_frozen_and_typed():
    advice = advise("c.s.v", workspace(BUSY), CONFIG, today=TODAY, quiet=True)
    assert isinstance(advice, Advice)
    assert isinstance(advice.dependencies[0], DependencyRun)
    with pytest.raises(Exception):
        advice.view = "other"


def test_default_workspace_requires_the_environment(monkeypatch):
    monkeypatch.delenv("WAREHOUSE_ID", raising=False)
    with pytest.raises(ValueError, match="WAREHOUSE_ID"):
        advise("c.s.v", None, CONFIG, today=TODAY, quiet=True)
