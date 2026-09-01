"""Configuration. Exactly three parameters — ask before adding a fourth."""

from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Config:

    days_history: int = 30
    buffer_min: int = 15
    timezone: str = "Asia/Kolkata"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


DEFAULT = Config()
