"""Shim so the repo-relative path keeps working.

The real entry point is the `view-scheduler` console script, which the package
installs from view_scheduler.cli:main. This file exists only so that
`PYTHONPATH=src python tools/analyze.py <view>` still does what it always did.
"""
from view_scheduler.cli import main

raise SystemExit(main())
