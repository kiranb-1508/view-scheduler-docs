"""View materialization scheduler.

Two tools for a Unity Catalog view:

  * `analyze` — when, each day, the view can be materialized with every
    dependency already refreshed. Computed from history. ADVISORY.
  * `can_refresh` — a runtime GATE. Right now, has every dependency of this
    view refreshed recently enough? READY or BLOCKED.

READ ONLY. This package never writes, alters, optimizes, vacuums or drops a
user table. Metadata and history reads only.

`analyze` is advisory: its number comes from history, not from today's state.
For a runtime check, use `can_refresh`, the gate. It reads each dependency's
actual last write and blocks when one is stale. See README.md.
"""

from .client import Workspace
from .config import Config
from .gate import Dependency, GateResult, ViewResult, can_refresh
from .run import analyze

__all__ = [
    "analyze",
    "can_refresh",
    "Config",
    "Dependency",
    "GateResult",
    "ViewResult",
    "Workspace",
]
