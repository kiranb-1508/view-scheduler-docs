"""Compute mode: which engine answers the history reads.

Resolution order: an explicit warehouse_id, then $WAREHOUSE_ID, then an active
Spark session, then an error that says exactly what is missing. Metadata reads
always go through the SDK SQL client under either mode; only the history
statement switches engines.

The verdicts must not depend on the mode. The last test pins that: the same
history, the same statement text, must classify identically through both
engines, so the two paths cannot drift apart.
"""

from datetime import datetime, timedelta, timezone

from view_scheduler.client import Workspace, read_latest_commit
from view_scheduler.config import Config
from tests import fixtures as f

UTC = timezone.utc
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
HOURS_AGO = 8


class FakeRow:
    """Just enough of a pyspark Row: `asDict()`."""

    def __init__(self, mapping):
        self._mapping = mapping

    def asDict(self):
        return dict(self._mapping)


class ColumnLike:
    """A pyspark Column mimic.

    Real Column objects do not raise AttributeError for a missing attribute;
    they return a callable that fails when called — which is why a transport
    bug reads as `'Column' object is not callable` rather than 'no attribute'.
    """

    def __getattr__(self, name):
        def not_a_row():
            raise TypeError("'Column' object is not callable")
        return not_a_row


class FakeDataFrame:
    """Mimics a pyspark DataFrame: `collect()` yields Rows. Iterating does
    NOT — pyspark 4 yields Columns, which is exactly the trap the transport
    must avoid."""

    def __init__(self, rows):
        self._rows = rows

    def collect(self):
        return [FakeRow(r) for r in self._rows]

    def __iter__(self):
        return iter([ColumnLike() for _ in self._rows])


class FakeSpark:
    """The SparkSession surface the Spark mode touches: `sql()` only."""

    def __init__(self, answers):
        self.answers = answers            # statement substring -> rows
        self.statements: list[str] = []

    def sql(self, statement: str):
        self.statements.append(statement)
        for key, rows in self.answers.items():
            if key in statement:
                return FakeDataFrame(rows)
        raise AssertionError(f"no answer staged for: {statement[:80]}")


class FakeSdk:
    """The WorkspaceClient surface `get_table` touches, per engine."""

    def __init__(self, tables):
        self._tables = tables
        self._api_client = self

    @property
    def api_client(self):
        return self._FakeApi(self._tables)

    def do(self, method, path):
        raise AssertionError("use api_client.do, not the client itself")

    class _FakeApi:
        def __init__(self, tables):
            self._tables = tables

        def do(self, method, path):
            name = path.rsplit("/", 1)[-1]
            if name not in self._tables:
                raise KeyError(name)
            return self._tables[name]


def epoch_row(version, dt, operation, params, metrics):
    """One history row as Spark returns it: ints where Spark gives ints."""
    return {"version": int(version), "ts_epoch": int(dt.timestamp()),
            "operation": operation, "operationParameters": params,
            "operationMetrics": metrics}


def warehouse_row(version, dt, operation, params, metrics):
    """The same row as the Statement API returns it: every value a string."""
    return {"version": str(version), "ts_epoch": str(int(dt.timestamp())),
            "operation": operation, "operationParameters": params,
            "operationMetrics": metrics}


LATEST = NOW - timedelta(hours=HOURS_AGO)
WRITE_SPARK = [epoch_row(0, LATEST, "WRITE",
                         '{"mode": "Overwrite"}', '{"numOutputRows": "100"}')]
WRITE_WAREHOUSE = [warehouse_row(0, LATEST, "WRITE",
                                 '{"mode": "Overwrite"}',
                                 '{"numOutputRows": "100"}')]
TABLES = {"c.s.v": f.uc_view("c.s.v", ["c.s.t"]),
          "c.s.t": f.uc_table("c.s.t")}


# --- resolution order --------------------------------------------------------

def test_explicit_warehouse_wins():
    assert Workspace(warehouse_id="wh-123").warehouse_id == "wh-123"


def test_env_warehouse_beats_spark(monkeypatch):
    monkeypatch.setenv("WAREHOUSE_ID", "wh-env")
    monkeypatch.setattr("view_scheduler.client.active_spark", lambda: FakeSpark({}))
    ws = Workspace()
    assert ws.warehouse_id == "wh-env"
    assert ws.spark is None


def test_spark_is_used_when_no_warehouse_anywhere(monkeypatch):
    monkeypatch.delenv("WAREHOUSE_ID", raising=False)
    session = FakeSpark({})
    monkeypatch.setattr("view_scheduler.client.active_spark", lambda: session)
    ws = Workspace()
    assert ws.warehouse_id is None
    assert ws.spark is session


def test_error_names_every_missing_engine(monkeypatch):
    monkeypatch.delenv("WAREHOUSE_ID", raising=False)
    monkeypatch.setattr("view_scheduler.client.active_spark", lambda: None)
    try:
        Workspace()
    except ValueError as exc:
        message = str(exc)
        assert "warehouse" in message.lower()
        assert "spark" in message.lower()
    else:
        raise AssertionError("should say exactly what is missing")


# --- reads through Spark -----------------------------------------------------

def test_spark_rows_come_from_collect_not_from_iteration():
    """Spark 4 trap: iterating a DataFrame yields Columns, so the only correct
    row source is collect(). With the previous transport, every history read
    died with 'Column' object is not callable."""
    session = FakeSpark({"SELECT": [{"version": 1}]})
    ws = Workspace(spark=session)
    assert ws.sql("SELECT 1", "probe") == [{"version": 1}]


def test_reads_history_through_spark(monkeypatch):
    """`read_latest_commit` turns an epoch int into an aware UTC datetime."""
    monkeypatch.delenv("WAREHOUSE_ID", raising=False)
    session = FakeSpark({"DESCRIBE HISTORY": WRITE_SPARK})
    monkeypatch.setattr("view_scheduler.client.active_spark", lambda: session)

    commit = read_latest_commit(Workspace(), "c.s.t")
    assert commit is not None
    assert commit.tzinfo is not None
    assert commit.astimezone(UTC) == LATEST
    assert "DESCRIBE HISTORY" in session.statements[0]


# --- the two engines must agree ----------------------------------------------

class FakeTableWS:
    """Warehouse-shaped Workspace: metadata + rows list from sql()."""

    warehouse_id = "fake"

    def __init__(self, rows):
        self._rows = rows

    def get_table(self, name):
        return TABLES[name]

    def sql(self, statement, target):
        return [dict(r) for r in self._rows]


def test_both_modes_classify_the_same_history_the_same(monkeypatch):
    """One gate call per engine, identical history, identical verdicts.

    Warehouse: a fully faked Workspace, as the rest of the suite does. Spark:
    the real Workspace resolving to a fake session, with the SDK client faked
    so the walk can resolve metadata. If the paths drift — say the Spark path
    drops operationParameters — the verdicts part ways here.
    """
    from view_scheduler.gate import can_refresh

    kwargs = dict(now=NOW, quiet=True, config=Config(timezone="UTC"))

    warehouse_result = can_refresh(
        "c.s.v", FakeTableWS(WRITE_WAREHOUSE), **kwargs)

    monkeypatch.delenv("WAREHOUSE_ID", raising=False)
    monkeypatch.setattr("view_scheduler.client.active_spark",
                        lambda: FakeSpark({"DESCRIBE HISTORY": WRITE_SPARK}))
    monkeypatch.setattr("view_scheduler.client.WorkspaceClient",
                        lambda: FakeSdk(TABLES))
    spark_result = can_refresh("c.s.v", None, **kwargs)

    wh_view, sp_view = warehouse_result.views[0], spark_result.views[0]
    assert wh_view.ready == sp_view.ready
    wh_dep, sp_dep = wh_view.dependencies[0], sp_view.dependencies[0]
    assert wh_dep.ok == sp_dep.ok
    assert wh_dep.last_refresh == sp_dep.last_refresh
    assert wh_dep.detail == sp_dep.detail
