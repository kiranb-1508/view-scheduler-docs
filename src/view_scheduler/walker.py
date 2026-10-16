"""Walk a view's dependencies down to the physical tables that gate it.

One question: given a view, which physical tables must be fresh before the
view can refresh?

The walk is depth-first. It has two branches:

  * A **VIEW** lists its dependencies. Go into each one.
  * A **physical table** (MANAGED, EXTERNAL, MATERIALIZED_VIEW, STREAMING_TABLE)
    ends the branch. Record it in `tables`.

`walk()` returns three lists:

  tables    the physical tables that gate the view
  blockers  problems that must stop the view: a cycle, an unreadable table, or
            a view that reports no dependencies at all
  notes     facts worth reporting, never worth blocking on: a function
            dependency has no refresh to wait for

Three rules matter. Each one fails closed — a false BLOCK is safe, a false
READY is the failure this whole project exists to prevent.

  * A table reached by two paths is recorded **once**. Every finished name is
    remembered in `done`.
  * A **cycle** blocks, and must not be mistaken for "already done". The names
    on the current path are remembered in `on_stack`; a repeat there is a cycle.
    One set cannot do both jobs: with a single "seen" set, `A -> B -> A` reads
    `A` as finished, the walk returns no tables, and an empty view passes as
    ready.
  * **Anything we cannot read blocks**, and the walk continues on the branches
    it can still read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .client import failure_category
from .models import Skipped

#: Types that stop the walk — something with a real commit history behind it.
GATING = {"MANAGED", "EXTERNAL", "MATERIALIZED_VIEW", "STREAMING_TABLE"}

MAX_DEPTH = 60

#: Fetch one table's metadata as the raw API dict, or raise. Injected so the
#: walk is testable with no network.
Fetch = Callable[[str], dict]


@dataclass(frozen=True)
class GatingTable:
    """A physical table that gates a view, with its Unity Catalog type."""

    name: str
    kind: str


@dataclass(frozen=True)
class WalkResult:
    """What one walk found. See the module docstring for the three lists."""

    tables: tuple[GatingTable, ...] = ()
    blockers: tuple[Skipped, ...] = ()
    notes: tuple[Skipped, ...] = ()


def walk(view: str, fetch: Fetch, max_depth: int = MAX_DEPTH) -> WalkResult:
    """Walk `view` down to the tables that gate it. Read one table with `fetch`.

    `fetch` returns a table's raw metadata dict, or raises. Inject a fake to
    test with no network. `max_depth` stops a pathologically deep graph.
    """
    return _Walk(fetch, max_depth).walk(view)


def _view_dependencies(meta: dict) -> tuple[list[str], list[Skipped], list[Skipped]]:
    """(table names, blockers, notes) listed by one VIEW's metadata.

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
            notes.append(Skipped(fn_name, "function dependency — nothing to refresh",
                                 blocks=False))
            continue
        blockers.append(Skipped(str(dependency)[:80], "unrecognised dependency shape"))
    return names, blockers, notes


class _Walk:
    """One depth-first walk. State lives on the instance, so the step methods
    do not have to thread four lists through every call.

    `done` and `on_stack` are the two sets. Their difference is the point:
    `done` is every name we have finished (a repeat is harmless); `on_stack` is
    the names on the path from the root to here (a repeat is a cycle).
    """

    def __init__(self, fetch: Fetch, max_depth: int):
        self.fetch = fetch
        self.max_depth = max_depth
        self.tables: list[GatingTable] = []
        self.blockers: list[Skipped] = []
        self.notes: list[Skipped] = []
        self.done: set[str] = set()
        self.on_stack: set[str] = set()

    def walk(self, view: str) -> WalkResult:
        """Walk from the root, then freeze what we found."""
        self._visit(view, 0)
        return WalkResult(tuple(self.tables), tuple(self.blockers),
                          tuple(self.notes))

    def _visit(self, name: str, depth: int) -> None:
        """Visit one name: skip it, block on it, or go deeper."""
        if name in self.done:
            return
        if name in self.on_stack:
            self.blockers.append(Skipped(
                name, "circular dependency — no physical table reached"))
            return
        if depth > self.max_depth:
            self.blockers.append(Skipped(
                name, f"depth cap {self.max_depth} exceeded"))
            self.done.add(name)
            return

        self.on_stack.add(name)
        try:
            self._visit_one(name, depth)
        finally:
            self.on_stack.discard(name)
            self.done.add(name)

    def _visit_one(self, name: str, depth: int) -> None:
        """Read this name's metadata and dispatch on what it is."""
        try:
            meta = self.fetch(name)
        except Exception as exc:
            self.blockers.append(Skipped(name, _reason(exc)))
            return
        if not isinstance(meta, dict):
            self.blockers.append(Skipped(name, "unreadable metadata (not an object)"))
            return

        kind = meta.get("table_type")
        if kind == "VIEW":
            self._visit_view(name, meta, depth)
        elif isinstance(kind, str) and kind in GATING:
            self.tables.append(GatingTable(name, kind))
        else:
            self.blockers.append(Skipped(
                name, f"not readable as a Delta table (type {kind})"))

    def _visit_view(self, name: str, meta: dict, depth: int) -> None:
        """Read a view's dependencies, then walk each one."""
        try:
            children, blocked, noted = _view_dependencies(meta)
        except Exception as exc:
            self.blockers.append(Skipped(
                name, f"unreadable metadata ({type(exc).__name__})"))
            return

        self.blockers.extend(blocked)
        self.notes.extend(noted)
        if not children and not noted:
            self.blockers.append(Skipped(
                name, "view reports no dependencies — cannot confirm freshness"))
        for child in children:
            self._visit(child, depth + 1)


def _reason(exc: Exception) -> str:
    """A short, plain reason for a metadata read that failed."""
    category = failure_category(exc)
    if category == "permission":
        return "permission denied"
    if category == "not_found":
        return "not found"
    return f"{type(exc).__name__}: {str(exc)[:120]}"
