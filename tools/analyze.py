"""Run the analyzer against a real view."""
import os
import sys

from scheduler import obs
from scheduler.client import Workspace
from scheduler.config import Config
from scheduler.run import analyze

obs.setup()
view = sys.argv[1]
ws = Workspace(warehouse_id=os.environ["WAREHOUSE_ID"])
text, _, _ = analyze(view, ws, Config())
print("\n" + text + "\n")
