"""Small, read-only view materialization scheduler.

The module has two I/O seams: ``fetch_meta`` for Unity Catalog metadata and
``fetch_history`` for DESCRIBE HISTORY rows. Everything else is deterministic
Python so it can be tested without Spark or a network connection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from statistics import median
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo


BATCH_TABLE_TYPES = frozenset(
    {
        "MANAGED",
        "EXTERNAL",
        "MATERIALIZED_VIEW",
        "METRIC_VIEW",
        "MANAGED_SHALLOW_CLONE",
        "EXTERNAL_SHALLOW_CLONE",
    }
)
STREAMING_TABLE_TYPES = frozenset({"STREAMING_TABLE"})
FULL_OPERATIONS = frozenset(
    {
        "CREATE TABLE AS SELECT",
        "CREATE OR REPLACE TABLE AS SELECT",
        "REPLACE TABLE AS SELECT",
        "REPLACE TABLE",
        "INSERT OVERWRITE",
        "RESTORE",
        "CLONE",
    }
)
INCREMENTAL_OPERATIONS = frozenset(
    {"APPEND", "COPY INTO", "MERGE", "UPDATE", "STREAMING UPDATE"}
)
ROW_METRIC_KEYS = (
    "numOutputRows",
    "numTargetRowsInserted",
    "numTargetRowsUpdated",
    "numTargetRowsCopied",
    "numTargetRowsDeleted",
    "numInsertedRows",
    "numUpdatedRows",
    "numDeletedRows",
)


@dataclass(frozen=True)
class Config:
    days_history: int = 30
    buffer_min: int = 15
    timezone: str = "Asia/Kolkata"

    def __post_init__(self) -> None:
        if self.days_history < 1:
            raise ValueError("days_history must be positive")
        if self.buffer_min < 0:
            raise ValueError("buffer_min must not be negative")
        ZoneInfo(self.timezone)

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True)
class Skipped:
    name: str
    reason: str


@dataclass(frozen=True)
class TableRef:
    name: str
    kind: str


@dataclass
class WalkResult:
    batch: list[TableRef] = field(default_factory=list)
    streaming: list[TableRef] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)


@dataclass(frozen=True)
class Classification:
    status: str
    qualifies: bool
    flag: str | None = None


@dataclass
class HistorySummary:
    finish_times: dict[date, int]
    ran_days: set[date]
    qualifying_timestamps: list[datetime]
    oldest_entry: date | None
    degraded: bool = False
    unclassified: bool = False


@dataclass
class TableAnalysis:
    ref: TableRef
    history: HistorySummary
    observed_weekdays: set[int]
    usual_min: int | None
    worst_min: int | None
    missed_days: int
    history_days: int
    stream_last: datetime | None = None
    stream_max_gap_min: int | None = None


@dataclass
class Recommendation:
    safe_min: int | None
    sooner_min: int | None
    per_weekday: dict[int, int]
    all_clear: dict[date, int]
    blocked: dict[date, list[str]]
    binding: dict[date, str]
    early_days: int = 0
    worst_gap_min: int = 0


@dataclass
class Report:
    view: str
    config: Config
    today: date
    recommendation: Recommendation
    dependencies: list[TableAnalysis]
    skipped: list[Skipped]


def _as_int(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return int(value or 0)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def rows_written(metrics: Mapping[str, Any]) -> int:
    """Return a positive number when any known row metric is positive."""

    return sum(max(0, _as_int(metrics.get(key))) for key in ROW_METRIC_KEYS)


def classify(row: Mapping[str, Any]) -> Classification:
    """Classify one DESCRIBE HISTORY row.

    A full replacement qualifies even when it produced zero rows. Incremental
    writes with missing metrics qualify but are marked degraded. A WRITE with
    no mode is retained as an unclassified qualifying write rather than being
    silently treated as a non-refresh.
    """

    operation = str(row.get("operation") or "").strip().upper()
    params = row.get("operationParameters") or {}
    mode = str(params.get("mode") or "").strip().upper()
    metrics = row.get("operationMetrics")

    if operation in FULL_OPERATIONS or (
        operation == "WRITE" and mode == "OVERWRITE"
    ):
        return Classification("full", True)

    if operation in INCREMENTAL_OPERATIONS or (
        operation == "WRITE" and mode in {"APPEND", "MERGE", "UPDATE"}
    ):
        if not metrics:
            return Classification("degraded", True, "metrics-degraded")
        if rows_written(metrics) > 0:
            return Classification("incremental", True)
        return Classification("ran-empty", False, "ran-empty")

    if operation == "WRITE":
        return Classification("unclassified", True, "unclassified-write")

    return Classification("ignored", False)


def _local_timestamp(value: datetime, zone: ZoneInfo) -> datetime:
    """Convert a timestamp to the configured zone.

    Spark returns a naive timestamp when the session timezone is pinned. In
    that case the value already represents local wall-clock time.
    """

    if value.tzinfo is None:
        return value.replace(tzinfo=zone)
    return value.astimezone(zone)


def _minute_of_day(value: datetime) -> int:
    return value.hour * 60 + value.minute


def finish_times(
    rows: Iterable[Mapping[str, Any]],
    today: date,
    config: Config,
) -> HistorySummary:
    """Build local-date finish times from raw history rows."""

    cutoff = today - timedelta(days=config.days_history - 1)
    finish: dict[date, int] = {}
    ran_days: set[date] = set()
    qualifying_timestamps: list[datetime] = []
    oldest: date | None = None
    degraded = False
    unclassified = False

    for row in rows:
        timestamp = row.get("timestamp")
        if not isinstance(timestamp, datetime):
            continue
        local = _local_timestamp(timestamp, config.zone)
        local_day = local.date()
        oldest = local_day if oldest is None else min(oldest, local_day)
        if local_day < cutoff or local_day > today:
            continue

        result = classify(row)
        if result.flag == "metrics-degraded":
            degraded = True
        if result.flag == "unclassified-write":
            unclassified = True
        if result.qualifies or result.status == "ran-empty":
            ran_days.add(local_day)
        if not result.qualifies:
            continue

        qualifying_timestamps.append(local)
        minute = _minute_of_day(local)
        finish[local_day] = min(finish.get(local_day, minute), minute)

    qualifying_timestamps.sort()
    return HistorySummary(
        finish_times=finish,
        ran_days=ran_days,
        qualifying_timestamps=qualifying_timestamps,
        oldest_entry=oldest,
        degraded=degraded,
        unclassified=unclassified,
    )


def _dedup_refs(refs: Iterable[TableRef]) -> list[TableRef]:
    result: list[TableRef] = []
    seen: set[str] = set()
    for ref in refs:
        if ref.name not in seen:
            result.append(ref)
            seen.add(ref.name)
    return result


def walk(
    fqn: str,
    fetch_meta: Callable[[str], Mapping[str, Any]],
    max_depth: int = 60,
) -> WalkResult:
    """Walk UC view dependencies with memoization and a cycle guard."""

    memo: dict[str, WalkResult] = {}
    active: set[str] = set()

    def visit(name: str, depth: int) -> WalkResult:
        if name in memo:
            return memo[name]
        if name in active:
            return WalkResult(skipped=[Skipped(name, "cycle")])
        if depth > max_depth:
            return WalkResult(skipped=[Skipped(name, "depth limit")])

        active.add(name)
        try:
            metadata = fetch_meta(name)
        except Exception as exc:  # API failures are reportable, not fatal.
            result = WalkResult(
                skipped=[Skipped(name, f"metadata error: {type(exc).__name__}")]
            )
            active.remove(name)
            memo[name] = result
            return result

        kind = str(metadata.get("table_type") or "UNKNOWN").upper()
        result = WalkResult()
        if kind == "VIEW":
            dependencies = (metadata.get("view_dependencies") or {}).get(
                "dependencies", []
            )
            for dependency in dependencies:
                child = dependency.get("table_full_name")
                if not child:
                    result.skipped.append(
                        Skipped(str(dependency), "function dependency")
                    )
                    continue
                child_result = visit(child, depth + 1)
                result.batch.extend(child_result.batch)
                result.streaming.extend(child_result.streaming)
                result.skipped.extend(child_result.skipped)
        elif kind in BATCH_TABLE_TYPES:
            result.batch.append(TableRef(name, kind))
        elif kind in STREAMING_TABLE_TYPES:
            result.streaming.append(TableRef(name, kind))
        else:
            result.skipped.append(Skipped(name, f"unsupported table type: {kind}"))

        active.remove(name)
        result.batch = _dedup_refs(result.batch)
        result.streaming = _dedup_refs(result.streaming)
        memo[name] = result
        return result

    result = visit(fqn, 0)
    result.batch = _dedup_refs(result.batch)
    result.streaming = _dedup_refs(result.streaming)
    return result


def _median_min(values: Iterable[int]) -> int | None:
    values = list(values)
    return round(median(values)) if values else None


def _stream_gap_stats(timestamps: Sequence[datetime]) -> tuple[datetime | None, int | None]:
    if not timestamps:
        return None, None
    if len(timestamps) == 1:
        return timestamps[-1], 0
    gaps = [
        round((right - left).total_seconds() / 60)
        for left, right in zip(timestamps, timestamps[1:])
    ]
    return timestamps[-1], max(gaps)


def _table_analysis(
    ref: TableRef,
    history: HistorySummary,
    today: date,
    config: Config,
) -> TableAnalysis:
    cutoff = today - timedelta(days=config.days_history - 1)
    observed_weekdays = {day.weekday() for day in history.finish_times}
    expected_start = max(cutoff, history.oldest_entry or today)
    expected_days = {
        expected_start + timedelta(days=offset)
        for offset in range((today - expected_start).days + 1)
        if (expected_start + timedelta(days=offset)).weekday() in observed_weekdays
    }
    missed_days = len(expected_days - history.ran_days) if observed_weekdays else 0
    minutes = list(history.finish_times.values())
    last, max_gap = _stream_gap_stats(history.qualifying_timestamps)

    return TableAnalysis(
        ref=ref,
        history=history,
        observed_weekdays=observed_weekdays,
        usual_min=_median_min(minutes) if ref.kind != "STREAMING_TABLE" else None,
        worst_min=max(minutes) if minutes and ref.kind != "STREAMING_TABLE" else None,
        missed_days=missed_days,
        history_days=(
            0
            if history.oldest_entry is None
            else min(config.days_history, (today - max(cutoff, history.oldest_entry)).days + 1)
        ),
        stream_last=last if ref.kind == "STREAMING_TABLE" else None,
        stream_max_gap_min=max_gap if ref.kind == "STREAMING_TABLE" else None,
    )


def recommend(
    tables: Sequence[TableAnalysis],
    today: date,
    config: Config,
) -> Recommendation:
    """Recommend times for batch tables using stale-ready semantics.

    If a table refreshed earlier in the window but not today, it is already as
    fresh as the available history can make it, so it contributes zero minutes.
    A table that has never had a qualifying refresh blocks that day instead of
    being mistaken for a table ready at midnight.
    """

    batch = [table for table in tables if table.ref.kind != "STREAMING_TABLE"]
    if not batch:
        return Recommendation(None, None, {}, {}, {}, {})

    cutoff = today - timedelta(days=config.days_history - 1)
    coverage_start = max(
        [max(cutoff, table.history.oldest_entry or today) for table in batch]
    )
    all_clear: dict[date, int] = {}
    blocked: dict[date, list[str]] = {}
    binding: dict[date, str] = {}

    for offset in range(config.days_history):
        day = cutoff + timedelta(days=offset)
        if day < coverage_start:
            continue
        ready: dict[str, int] = {}
        missing: list[str] = []
        for table in batch:
            finishes = table.history.finish_times
            if day in finishes:
                ready[table.ref.name] = finishes[day]
            elif any(previous < day for previous in finishes):
                ready[table.ref.name] = 0
            else:
                missing.append(table.ref.name)
        if missing:
            blocked[day] = missing
            continue
        all_clear[day] = max(ready.values())
        binding[day] = max(ready, key=ready.get)

    values = sorted(all_clear.values())
    if not values:
        return Recommendation(None, None, {}, {}, blocked, binding)

    safe_raw = values[-1]
    second_raw = values[-2] if len(values) > 1 else None
    per_weekday = {
        weekday: max(value for day, value in all_clear.items() if day.weekday() == weekday)
        + config.buffer_min
        for weekday in {day.weekday() for day in all_clear}
    }
    sooner_raw = None if second_raw is None else second_raw + config.buffer_min
    early_days = (
        0
        if sooner_raw is None
        else sum(value + config.buffer_min > sooner_raw for value in values)
    )
    return Recommendation(
        safe_min=safe_raw + config.buffer_min,
        sooner_min=sooner_raw,
        per_weekday=per_weekday,
        all_clear=all_clear,
        blocked=blocked,
        binding=binding,
        early_days=early_days,
        worst_gap_min=0 if second_raw is None else safe_raw - second_raw,
    )


def analyze(
    view_fqn: str,
    fetch_meta: Callable[[str], Mapping[str, Any]],
    fetch_history: Callable[[str], Iterable[Mapping[str, Any]]],
    today: date,
    config: Config | None = None,
) -> Report:
    """Run the complete analysis with injected external readers."""

    config = config or Config()
    walked = walk(view_fqn, fetch_meta)
    skipped = list(walked.skipped)
    analyses: list[TableAnalysis] = []
    refs = walked.batch + walked.streaming

    for ref in refs:
        try:
            rows = fetch_history(ref.name)
            history = finish_times(rows, today, config)
        except Exception as exc:
            skipped.append(Skipped(ref.name, f"history error: {type(exc).__name__}"))
            continue
        analyses.append(_table_analysis(ref, history, today, config))

    return Report(
        view=view_fqn,
        config=config,
        today=today,
        recommendation=recommend(analyses, today, config),
        dependencies=analyses,
        skipped=skipped,
    )


WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def format_minutes(value: int | None) -> str:
    if value is None:
        return "--"
    if value >= 24 * 60:
        return f"{value // 60:02d}:{value % 60:02d} (+next day)"
    return f"{value // 60:02d}:{value % 60:02d}"


def _run_label(table: TableAnalysis) -> str:
    if table.ref.kind == "STREAMING_TABLE":
        return "streaming"
    if not table.observed_weekdays:
        return "no qualifying runs"
    if len(table.observed_weekdays) == 7:
        return "daily"
    return ",".join(WEEKDAYS[index] for index in sorted(table.observed_weekdays))


def render(report: Report, today: date | None = None) -> str:
    """Render a report for a human operator."""

    today = report.today if today is None else today
    recommendation = report.recommendation
    lines = [
        f"VIEW   {report.view}",
        f"       {len(report.dependencies)} dependencies checked | "
        f"{len(report.skipped)} not checked | times {report.config.timezone}",
        "",
    ]

    today_time = recommendation.per_weekday.get(today.weekday(), recommendation.safe_min)
    if today_time is None:
        lines.append("       NO RECOMMENDATION")
        lines.append("       no day had a qualifying refresh for every batch dependency")
    else:
        lines.append(f"       RUN AT  {format_minutes(today_time)}  ({WEEKDAYS[today.weekday()]})")
        lines.append(
            f"       safe on {len(recommendation.all_clear)} evaluated days | "
            f"buffer {report.config.buffer_min} min"
        )
        if recommendation.sooner_min is not None:
            lines.append(
                f"       SOONER   {format_minutes(recommendation.sooner_min)} | "
                f"early on {recommendation.early_days} day(s) | "
                f"worst gap {recommendation.worst_gap_min} min"
            )
        if len(set(recommendation.per_weekday.values())) > 1:
            lines.append("       WEEKDAY TIMES")
            for weekday, minute in sorted(recommendation.per_weekday.items()):
                lines.append(f"         {WEEKDAYS[weekday]}  {format_minutes(minute)}")

    lines.extend(
        [
            "",
            "       EVIDENCE",
            "       dependency                    runs                 usual   worst   missed",
        ]
    )
    for table in sorted(report.dependencies, key=lambda item: item.ref.name):
        lines.append(
            f"       {table.ref.name:<29} { _run_label(table):<20} "
            f"{format_minutes(table.usual_min):>7} {format_minutes(table.worst_min):>7} "
            f"{table.missed_days:>7}"
        )

    warnings: list[str] = []
    for table in report.dependencies:
        if table.history.degraded:
            warnings.append(f"{table.ref.name}: metrics-degraded; empty metrics counted as refresh")
        if table.history.unclassified:
            warnings.append(f"{table.ref.name}: unclassified WRITE counted as refresh")
        if table.missed_days:
            warnings.append(
                f"{table.ref.name}: {table.missed_days} expected refresh day(s) had no write"
            )
        if table.history_days < report.config.days_history:
            warnings.append(
                f"{table.ref.name}: only {table.history_days} day(s) of history available"
            )
        if table.stream_max_gap_min is not None and table.stream_max_gap_min > 60:
            warnings.append(
                f"{table.ref.name}: streaming gap reached {table.stream_max_gap_min} minutes"
            )
    for day, names in sorted(recommendation.blocked.items()):
        warnings.append(f"{day}: no qualifying refresh yet for {', '.join(names)}")

    if report.skipped:
        lines.extend(["", "       NOT CHECKED"])
        for skipped in report.skipped:
            lines.append(f"       {skipped.name}: {skipped.reason}")
    if warnings:
        lines.extend(["", "       WARNINGS"])
        lines.extend(f"       ! {warning}" for warning in warnings)
    return "\n".join(lines)
