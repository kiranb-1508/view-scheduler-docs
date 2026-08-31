"""Phase 2 gate, real half: classify every commit on a real table and show the
verdict, so a misclassification is visible rather than silent."""
import os
import sys

from scheduler import obs
from scheduler.client import Workspace, read_history
from scheduler.history import classify, has_metrics, qualifying, rows_written
from scheduler.run import _read

obs.setup(level=40)   # quiet the per-call log; the table below is the output
ws = Workspace(warehouse_id=os.environ["WAREHOUSE_ID"])

for table in sys.argv[1:]:
    print(f"\n{table}")
    try:
        commits = _read(ws, table)
    except Exception as exc:
        print(f"  UNREADABLE: {str(exc)[:110]}")
        continue
    print(f"  {'V':<4}{'OPERATION':<26}{'MODE':<11}{'CLASS':<14}"
          f"{'ROWS':<7}{'METRICS':<9}QUALIFIES")
    for c in sorted(commits, key=lambda c: c.version, reverse=True):
        print(f"  {c.version:<4}{c.operation[:25]:<26}"
              f"{(c.params or {}).get('mode', '—'):<11}{classify(c):<14}"
              f"{rows_written(c.metrics):<7}"
              f"{('yes' if has_metrics(c) else 'NONE'):<9}"
              f"{'YES' if qualifying(c) else 'no'}")
