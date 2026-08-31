"""Configuration. Exactly three parameters — ask before adding a fourth."""

from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Config:
    """The three knobs.

    days_history: how far back to look for refresh commits.
    buffer_min:   margin added to the worst observed all-clear time.
    timezone:     the clock the recommendation is expressed in. Pinned
                  explicitly because Databricks TIMESTAMP renders in
                  spark.sql.session.timeZone, which defaults to UTC — inherit
                  it and the same view returns different times on different
                  warehouses.
    """

    days_history: int = 30
    buffer_min: int = 15
    timezone: str = "Asia/Kolkata"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


DEFAULT = Config()
