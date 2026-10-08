"""Walk a view's dependency tree down to the physical tables that gate it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from .client import failure_category
from .models import Skipped

#: Types that stop the walk — something with a real commit history behind it.
GATING = {"MANAGED", "EXTERNAL", "MATERIALIZED_VIEW", "STREAMING_TABLE"}

MAX_DEPTH = 60

#: Fetch one table's metadata as the raw API dict, or raise. Injected so the
#: walk is testable with no network.
Fetch = Callable[[str], dict]


@dataclass(frozen=True)
class GatingRef:
    """A physical table that gates a view, with its Unity Catalog type."""

    name: str
    kind: str


def _dependency_names(meta: dict) -> tuple[list[str], list[Skipped], list[Skipped]]:
    """(table names, blockers, notes) for a VIEW payload.

    A function dependency is a NOTE: a SQL UDF has no refresh to wait for.
    Anything we cannot read is a BLOCKER, because the gate fails closed.
    """
    names: list[str] = []
    blockers: list[Skipped] = []
    notes: list[Skipped] = []

    dependencies = (meta.get("view_dependencies") or {}).get("dependencies") or []
    if not isinstance(dependencies, list):
        raise ValueError("view_dependencies.dependencies is not a list")

    for dependency in dependencies:
        if not isinstance(dependency, dict):
            blockers.append(Skipped(str(dependency)[:80], "unreadable dependency entry"))
            continue
        table = dependency.get("table") or {}
        full_name = table.get("table_full_name") if isinstance(table, dict) else None
        if isinstance(full_name, str) and full_name:
            names.append(full_name)
            continue
        function = dependency.get("function") or {}
        fn_name = (function.get("function_full_name")
                   if isinstance(function, dict) else None)
        if isinstance(fn_name, str) and fn_name:
            notes.append(Skipped(fn_name, "function dependency — nothing to refresh"))
            continue
        blockers.append(Skipped(str(dependency)[:80], "unrecognised dependency shape"))
    return names, blockers, notes


def walk_refs(fqn: str, fetch: Fetch, max_depth: int = MAX_DEPTH
              ) -> tuple[list[GatingRef], list[Skipped], list[Skipped]]:
    """(gating tables, blockers, notes) for a view.

    View dependencies are followed; a physical table ends a branch. A cycle or a
    dead end is a BLOCKER. Deduplicating on "seen" alone would report a cyclic
    graph as having nothing to wait for, which would fail OPEN.
    """
    gating: list[GatingRef] = []
    blockers: list[Skipped] = []
    notes: list[Skipped] = []
    resolved: set[str] = set()
    active: set[str] = set()

    def visit(name: str, depth: int) -> None:
        if name in resolved:
            return
        if name in active:
            blockers.append(Skipped(
                name, "circular dependency — no physical table reached"))
            return
        if depth > max_depth:
            blockers.append(Skipped(name, f"depth cap {max_depth} exceeded"))
            resolved.add(name)
            return

        active.add(name)
        try:
            try:
                meta = fetch(name)
            except Exception as exc:
                blockers.append(Skipped(name, _reason(exc)))
                return
            if not isinstance(meta, dict):
                blockers.append(Skipped(name, "unreadable metadata (not an object)"))
                return

            kind = meta.get("table_type")
            if kind == "VIEW":
                try:
                    children, child_blockers, child_notes = _dependency_names(meta)
                except Exception as exc:
                    blockers.append(Skipped(
                        name, f"unreadable metadata ({type(exc).__name__})"))
                    return
                blockers.extend(child_blockers)
                notes.extend(child_notes)
                if not children and not child_notes:
                    blockers.append(Skipped(
                        name, "view reports no dependencies — cannot confirm freshness"))
                for child in children:
                    visit(child, depth + 1)
            elif isinstance(kind, str) and kind in GATING:
                gating.append(GatingRef(name, kind))
            else:
                blockers.append(Skipped(
                    name, f"not readable as a Delta table (type {kind})"))
        finally:
            active.discard(name)
            resolved.add(name)

    visit(fqn, 0)
    return gating, blockers, notes


def walk(fqn: str, fetch: Fetch, max_depth: int = MAX_DEPTH
         ) -> tuple[list[str], list[Skipped]]:
    """(gating table names, everything that was skipped). Used by analyze()."""
    refs, blockers, notes = walk_refs(fqn, fetch, max_depth)
    return [ref.name for ref in refs], blockers + notes


def _reason(exc: Exception) -> str:
    category = failure_category(exc)
    if category == "permission":
        return "permission denied"
    if category == "not_found":
        return "not found"
    return f"{type(exc).__name__}: {str(exc)[:120]}"
