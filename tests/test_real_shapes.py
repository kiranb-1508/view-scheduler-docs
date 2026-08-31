"""Regression tests over commit shapes captured from real Databricks writers.

These are not invented. Each row below was produced by running the statement in
the comment against a live workspace (the same operation classes the rig's
write_slot emits), then reading it back with DESCRIBE HISTORY. They exist so a
future change to the classifier cannot quietly break against the shapes that
actually occur.
"""

from datetime import timezone

from scheduler.history import classify, qualifying
from scheduler.models import Commit
from tests import fixtures as f

UTC = timezone.utc


def c(operation, params, metrics, version=0) -> Commit:
    return Commit(version, f.at("2026-08-31", "10:55"), operation, params, metrics)


#: (statement that produced it, commit, expected class, expected qualifying)
CAPTURED = [
    # CREATE OR REPLACE TABLE ... AS SELECT ... FROM range(100)
    ("ctas", c("CREATE OR REPLACE TABLE AS SELECT", {},
               {"numOutputRows": "100", "numFiles": "1"}), "full_replace", True),
    # INSERT OVERWRITE ... SELECT ... FROM range(50)
    ("insert overwrite", c("WRITE", {"mode": "Overwrite"},
                           {"numOutputRows": "50"}), "full_replace", True),
    # INSERT OVERWRITE ... FROM range(0)  — a recompute that produced nothing
    ("zero-row overwrite", c("WRITE", {"mode": "Overwrite"},
                             {"numOutputRows": "0"}), "full_replace", True),
    # INSERT INTO ... SELECT ... FROM range(10, 20)
    ("append", c("WRITE", {"mode": "Append"}, {"numOutputRows": "10"}),
     "incremental", True),
    # INSERT INTO ... FROM range(0)
    ("append zero rows", c("WRITE", {"mode": "Append"}, {"numOutputRows": "0"}),
     "incremental", False),
    # UPDATE ... SET v = v + 1 WHERE id < 5
    ("update", c("UPDATE", {}, {"numUpdatedRows": "5"}), "incremental", True),
    # MERGE INTO ... WHEN MATCHED / WHEN NOT MATCHED
    ("merge", c("MERGE", {}, {"numTargetRowsUpdated": "5",
                              "numTargetRowsInserted": "15"}), "incremental", True),
    # DELETE FROM ... WHERE id = 1
    ("delete", c("DELETE", {}, {"numDeletedRows": "1"}), "ignore", False),
    ("optimize", c("OPTIMIZE", {}, {"numRemovedFiles": "3"}), "ignore", False),
    ("set tblproperties", c("SET TBLPROPERTIES", {}, None), "ignore", False),
]


def test_every_captured_commit_classifies_as_observed():
    for label, commit, expected_class, expected_qualifying in CAPTURED:
        assert classify(commit) == expected_class, label
        assert qualifying(commit) is expected_qualifying, label


def test_the_same_operation_name_carries_opposite_verdicts():
    """The load-bearing fact, in the shapes a real warehouse emits: INSERT
    OVERWRITE and INSERT INTO both surface as a plain WRITE, and only the mode
    separates a full rebuild from an appended nothing."""
    overwrite = c("WRITE", {"mode": "Overwrite"}, {"numOutputRows": "0"})
    append = c("WRITE", {"mode": "Append"}, {"numOutputRows": "0"})
    assert overwrite.operation == append.operation == "WRITE"
    assert qualifying(overwrite) and not qualifying(append)


def test_classifying_by_name_alone_would_lose_most_real_refreshes():
    """Measured against the seeded tables: name-only matching finds 1 of the 3
    real refreshes on the overwrite-fed table, because the two that matter are
    called WRITE. That is the silent dormant-table failure."""
    name_only = {"CREATE OR REPLACE TABLE AS SELECT", "MERGE", "UPDATE",
                 "INSERT OVERWRITE", "COPY INTO"}
    overwrite_fed = [commit for label, commit, _, _ in CAPTURED
                     if label in ("ctas", "insert overwrite", "zero-row overwrite")]
    assert sum(1 for x in overwrite_fed if x.operation in name_only) == 1
    assert sum(1 for x in overwrite_fed if qualifying(x)) == 3
