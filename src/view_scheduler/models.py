"""Data shapes shared across modules. No logic, no I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Commit:
    """One row of DESCRIBE HISTORY or one MV refresh from the event log."""

    version: int
    timestamp: datetime
    operation: str
    params: dict[str, str] = field(default_factory=dict)
    metrics: dict[str, str] | None = None


@dataclass(frozen=True)
class Skipped:
    """A dependency that could not be resolved, and why. Never dropped silently.

    `blocks` says whether the missing evidence invalidates a recommended time.
    A table we could not read does — a time computed without one input is not a
    safe time. A function dependency does not: a SQL UDF has no refresh to wait
    for, so it never removes the headline.
    """

    name: str
    reason: str
    blocks: bool = True


@dataclass(frozen=True)
class TableHistory:
    """What we learned about one gating table."""

    name: str
    finish_times: dict = field(default_factory=dict)   # date -> aware datetime
    metrics_degraded: bool = False
    continuous: bool = False       # refreshed many times a day; finish is a floor
    unknown_ops: tuple = ()        # operation names we could not classify
    multi_daily: bool = False      # refreshed 2+ times on a typical active day
    daily_slots: tuple = ()        # median finish times (datetime.time) per daily run
