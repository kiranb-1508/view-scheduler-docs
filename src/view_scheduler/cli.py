"""Command line entry point: `view-scheduler <catalog.schema.view>`."""

from __future__ import annotations

import argparse
import os
import sys

from . import obs
from .client import Workspace
from .config import Config
from .run import analyze


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="view-scheduler",
        description="Recommend the earliest daily time a Unity Catalog view can "
                    "be materialized with every dependency already refreshed.",
        epilog="ADVISORY ONLY: nothing verifies at runtime that dependencies "
               "refreshed. A wrong recommendation publishes stale data silently.",
    )
    parser.add_argument("view", help="fully qualified view, e.g. catalog.schema.view")
    parser.add_argument(
        "--warehouse-id", default=os.environ.get("WAREHOUSE_ID"),
        help="SQL warehouse to run history reads against "
             "(default: $WAREHOUSE_ID)")
    # The three Config fields, and deliberately no fourth — see config.py.
    parser.add_argument("--days", type=int, default=Config.days_history,
                        metavar="N", help="days of history to read")
    parser.add_argument("--buffer", type=int, default=Config.buffer_min,
                        metavar="MIN", help="minutes added to the worst all-clear")
    parser.add_argument("--timezone", default=Config.timezone,
                        help="timezone the recommendation is expressed in")
    parser.add_argument("--quiet", action="store_true",
                        help="suppress the per-call log; print only the report")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.warehouse_id:
        # Fail on the missing input rather than deep inside the first SQL call.
        print("error: no warehouse. Pass --warehouse-id or set $WAREHOUSE_ID.",
              file=sys.stderr)
        return 2

    obs.setup(level=40 if args.quiet else 20)
    config = Config(days_history=args.days, buffer_min=args.buffer,
                    timezone=args.timezone)
    workspace = Workspace(warehouse_id=args.warehouse_id)

    try:
        text, _, _ = analyze(args.view, workspace, config)
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print("\n" + text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
