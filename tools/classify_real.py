"""Classify every commit on a real table and show the verdict."""
import os
import sys

from view_scheduler import obs
from view_scheduler.client import Workspace
from view_scheduler.history import classify, has_metrics, qualifying
from view_scheduler.run import _read

if len(sys.argv) < 2:
    sys.exit("usage: classify_real.py <catalog.schema.table> ...")

obs.setup(level=40)
ws = Workspace(warehouse_id=os.environ["WAREHOUSE_ID"])

for table in sys.argv[1:]:
    print(f"\n{table}")
    try:
        commits = _read(ws, table)
    except Exception as exc:
        print(f"  UNREADABLE: {str(exc)[:110]}")
        continue
    print(f"  {'V':<4}{'OPERATION':<26}{'MODE':<11}{'CLASS':<14}"
          f"{'METRICS':<9}QUALIFIES")
    for c in sorted(commits, key=lambda c: c.version, reverse=True):
        print(f"  {c.version:<4}{c.operation[:25]:<26}"
              f"{(c.params or {}).get('mode', '—'):<11}{classify(c):<14}"
              f"{('yes' if has_metrics(c) else 'NONE'):<9}"
              f"{'YES' if qualifying(c) else 'no'}")
