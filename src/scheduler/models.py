"""Normalized shapes the pure functions work on.

The I/O layer converts whatever DESCRIBE HISTORY or the event log returns into
these, so every computation downstream is testable with no Spark session and no
network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Commit:
    """One row of DESCRIBE HISTORY, or one completed materialized-view refresh.

    metrics is None when the writer emitted none at all (ADF and Synapse Link
    write through non-Spark paths and may emit nothing). That is distinct from
    an empty dict, which some Spark operations legitimately produce; both are
    treated as degraded, but only None means "the field was absent".
    """

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


@dataclass(frozen=True)
class TableHistory:
    """What we learned about one gating table."""

    name: str
    finish_times: dict = field(default_factory=dict)   # date -> aware datetime
    metrics_degraded: bool = False
