"""The seven job crons in the DABs bundle must match plan.SLOTS exactly.

Nothing else enforces this coupling. A fat-fingered cron produces a slot that
silently never fires, and with the rig running unattended for 30 days that is
discovered only at the end — every day until then is lost evidence. This turns
a 30-day-latency failure into a test failure.

Parsed with a regex rather than a YAML library so the suite needs no extra
dependency (CI installs only databricks-sdk and pytest).
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, "rig")

import plan  # noqa: E402

BUNDLE = Path(__file__).resolve().parent.parent / "rig" / "databricks.yml"


def crons_in_bundle() -> list[str]:
    text = BUNDLE.read_text()
    return re.findall(r'quartz_cron_expression:\s*"([^"]+)"', text)


def slot_to_quartz(slot: str) -> str:
    hour, minute = (int(part) for part in slot.split(":"))
    return f"0 {minute} {hour} * * ?"


def test_every_slot_has_exactly_one_matching_cron():
    found = crons_in_bundle()
    expected = {slot_to_quartz(s) for s in plan.SLOTS}
    assert len(found) == len(plan.SLOTS), (
        f"{len(plan.SLOTS)} slots but {len(found)} crons in the bundle")
    assert set(found) == expected, (
        f"cron/slot mismatch:\n  only in bundle: {set(found) - expected}\n"
        f"  only in SLOTS:  {expected - set(found)}")


def test_no_two_jobs_share_a_cron():
    found = crons_in_bundle()
    assert len(found) == len(set(found)), "duplicate cron — two jobs on one slot"


def test_bundle_timezone_matches_the_config():
    from scheduler.config import DEFAULT
    text = BUNDLE.read_text()
    zones = set(re.findall(r'timezone_id:\s*(\S+)', text))
    assert zones == {DEFAULT.timezone}, (
        f"bundle timezones {zones} != config {DEFAULT.timezone}")
