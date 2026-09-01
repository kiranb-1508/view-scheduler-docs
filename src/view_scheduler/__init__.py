"""View materialization scheduler.

Given a Unity Catalog view, work out the earliest time each day its
materialization can run with every dependency already refreshed.

READ ONLY. This package never writes, alters, optimizes, vacuums or drops a
user table. Metadata and history reads only.

The recommendation is ADVISORY: nothing here verifies at runtime that
dependencies actually refreshed, so a wrong number publishes stale data
silently. See README.md.
"""

from .client import Workspace
from .config import Config
from .run import analyze

__all__ = ["analyze", "Config", "Workspace"]
