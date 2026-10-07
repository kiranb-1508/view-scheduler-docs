"""Configuration. Keep it small — ask before adding a field."""

from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Config:

    days_history: int = 30
    buffer_min: int = 15
    timezone: str = "Asia/Kolkata"
    #: Gate only. A dependency is fresh when it wrote within this many hours.
    fresh_hours: int = 24

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


DEFAULT = Config()
