"""Structured logging: one line per external call, with target and elapsed ms."""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from typing import Any, Iterator

logger = logging.getLogger("view_scheduler")


def setup(level: int = logging.INFO) -> None:
    """Send one JSON line per external call to stderr. Safe to call twice."""
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(level)


@contextmanager
def call(kind: str, target: str, **extra: Any) -> Iterator[dict]:
    """Time one external call and log it, whether it succeeds or raises."""
    started = time.monotonic()
    record: dict[str, Any] = {"call": kind, "target": target, **extra}
    try:
        yield record
    except Exception as exc:
        record["outcome"] = "error"
        record["error"] = f"{type(exc).__name__}: {exc}"
        raise
    else:
        record.setdefault("outcome", "ok")
    finally:
        record["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
        logger.info(json.dumps(record, default=str))
