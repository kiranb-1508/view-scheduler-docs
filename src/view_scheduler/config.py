"""Configuration. Keep it small — ask before adding a field."""

from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Config:
    """The four settings. Nothing else is tunable without a decision.

    days_history, buffer_min, and timezone drive the advisor. fresh_hours
    drives the gate only. Ask before adding a fifth field.
    """

    days_history: int = 30
    buffer_min: int = 15
    timezone: str = "Asia/Kolkata"
    #: Gate only. A dependency is fresh when it wrote within this many hours.
    fresh_hours: int = 24

    def __post_init__(self) -> None:
        if self.fresh_hours <= 0:
            raise ValueError(f"fresh_hours must be positive, got {self.fresh_hours}")
        if self.days_history <= 0:
            raise ValueError(f"days_history must be positive, got {self.days_history}")

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


DEFAULT = Config()
