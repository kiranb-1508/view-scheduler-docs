"""Databricks notebook adapter for the V2 library.

This file is intentionally thin. Import it from a Databricks notebook and call
``run(view_fqn, spark)``. The library itself does not import Databricks SDK or
Spark, which keeps its tests local and deterministic.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from databricks.sdk import WorkspaceClient

try:
    from .lazy_scheduler import Config, Report, analyze, render
except ImportError:  # Supports running this file directly with %run.
    from lazy_scheduler import Config, Report, analyze, render


def _quote_fqn(fqn: str) -> str:
    return ".".join(f"`{part.replace('`', '``')}`" for part in fqn.split("."))


def run(
    view_fqn: str,
    spark: Any,
    today: date | None = None,
    config: Config | None = None,
) -> Report:
    config = config or Config()
    today = today or date.today()
    spark.conf.set("spark.sql.session.timeZone", config.timezone)
    client = WorkspaceClient()

    def fetch_meta(name: str) -> dict[str, Any]:
        return client.tables.get(full_name=name).as_dict()

    def fetch_history(name: str) -> list[dict[str, Any]]:
        query = f"DESCRIBE HISTORY {_quote_fqn(name)}"
        return [row.asDict(recursive=True) for row in spark.sql(query).collect()]

    report = analyze(view_fqn, fetch_meta, fetch_history, today, config)
    print(render(report, today))
    return report
