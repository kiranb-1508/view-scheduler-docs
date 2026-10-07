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
    """A dependency that could not be resolved, and why. Never dropped silently."""

    name: str
    reason: str
    #: True when the skip is a note, not a blocker — a function dependency has no
    #: history to read, but it also has nothing to wait for.
    benign: bool = False


@dataclass(frozen=True)
class TableHistory:
    """What we learned about one gating table."""

    name: str
    finish_times: dict = field(default_factory=dict)   # date -> aware datetime
    metrics_degraded: bool = False
    continuous: bool = False       # refreshed many times a day; finish is a floor
    unknown_ops: tuple = ()        # operation names we could not classify
