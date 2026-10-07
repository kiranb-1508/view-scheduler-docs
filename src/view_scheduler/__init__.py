"""View materialization scheduler.

Given a Unity Catalog view, work out the earliest time each day its
materialization can run with every dependency already refreshed.

READ ONLY. This package never writes, alters, optimizes, vacuums or drops a
user table. Metadata and history reads only.

`analyze` is ADVISORY: its number comes from history, not from today's state.
For a runtime check, use `can_refresh`, the gate. It reads each dependency's
actual last write and blocks when one is stale. See README.md.
"""

from .client import Workspace
from .config import Config
from .gate import GateResult, can_refresh
from .run import analyze

__all__ = ["analyze", "can_refresh", "Config", "GateResult", "Workspace"]
