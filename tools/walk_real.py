"""Phase 1 gate, real half: walk a real view and print both lists."""
import sys

from scheduler import obs
from scheduler.client import Workspace
from scheduler.walker import walk

obs.setup()
target = sys.argv[1]
ws = Workspace()
gating, skipped = walk(target, ws.get_table)
print(f"\nVIEW {target}")
print(f"  gating tables ({len(gating)}):")
for name in gating:
    print(f"    {name}")
print(f"  skipped ({len(skipped)}):")
for item in skipped:
    print(f"    {item.name} — {item.reason}")
