"""View materialization scheduler.

Two tools for a Unity Catalog view:

  * `advise` — suggest the time each day the view can run with every upstream
    job already finished. Computed from history. ADVISORY.
  * `can_refresh` — a runtime GATE. Right now, has every dependency of this
    view refreshed recently enough? READY or BLOCKED.

READ ONLY. This package never writes, alters, optimizes, vacuums or drops a
user table. Metadata and history reads only.

`advise` predicts. `can_refresh` checks. Use the first to set the schedule,
the second to guard the run. See README.md.
"""

from .advice import Advice, DependencyRun, advise
from .client import Workspace
from .config import Config
from .gate import Dependency, GateResult, ViewResult, can_refresh
from .run import analyze

__all__ = [
    "advise",
    "Advice",
    "analyze",
    "can_refresh",
    "Config",
    "Dependency",
    "DependencyRun",
    "GateResult",
    "ViewResult",
    "Workspace",
]
