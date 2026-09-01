"""Walk a view's dependency tree down to the physical tables that gate it."""

from __future__ import annotations

from typing import Callable, Iterable

from .client import failure_category
from .models import Skipped

#: Types that stop the walk — something with a real commit history behind it.
GATING = {"MANAGED", "EXTERNAL", "MATERIALIZED_VIEW", "STREAMING_TABLE"}

MAX_DEPTH = 60

#: Fetch one table's metadata as the raw API dict, or raise. Injected so the
#: walk is testable with no network.
Fetch = Callable[[str], dict]


def _dependency_names(meta: dict) -> tuple[list[str], list[Skipped]]:
    """Extract table names from view_dependencies; skip function dependencies."""
    names: list[str] = []
    skipped: list[Skipped] = []
    deps: Iterable[dict] = (meta.get("view_dependencies") or {}).get("dependencies") or []
    for dep in deps:
        table = dep.get("table") or {}
        full_name = table.get("table_full_name")
        if full_name:
            names.append(full_name)
            continue
        function = dep.get("function") or {}
        fn_name = function.get("function_full_name")
        if fn_name:
            skipped.append(Skipped(fn_name, "function dependency — no history to read"))
        else:
            skipped.append(Skipped(str(dep)[:80], "unrecognized dependency shape"))
    return names, skipped


def walk(fqn: str, fetch: Fetch, max_depth: int = MAX_DEPTH) -> tuple[list[str], list[Skipped]]:
    """Return (gating tables, skipped) for a view. Failures are recorded, not fatal."""
    gating: list[str] = []
    skipped: list[Skipped] = []
    seen: set[str] = set()

    def visit(name: str, depth: int) -> None:
        if name in seen:
            return
        seen.add(name)
        if depth > max_depth:
            skipped.append(Skipped(name, f"depth cap {max_depth} exceeded"))
            return
        try:
            meta = fetch(name)
        except Exception as exc:
            skipped.append(Skipped(name, _reason(exc)))
            return

        kind = meta.get("table_type")
        if kind == "VIEW":
            names, fn_skipped = _dependency_names(meta)
            skipped.extend(fn_skipped)
            if not names and not fn_skipped:
                skipped.append(Skipped(name, "view reports no dependencies"))
            for child in names:
                visit(child, depth + 1)
        elif kind in GATING:
            gating.append(name)
        else:
            skipped.append(Skipped(name, f"not readable as a Delta table (type {kind})"))

    visit(fqn, 0)
    return gating, skipped


def _reason(exc: Exception) -> str:
    category = failure_category(exc)
    if category == "permission":
        return "permission denied"
    if category == "not_found":
        return "not found"
    return f"{type(exc).__name__}: {str(exc)[:120]}"
