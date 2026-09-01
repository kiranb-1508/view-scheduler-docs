"""Walk a real view's dependencies and print both lists."""
import sys

from view_scheduler import obs
from view_scheduler.client import Workspace
from view_scheduler.walker import walk

if len(sys.argv) < 2:
    sys.exit("usage: walk_real.py <catalog.schema.view>")

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
