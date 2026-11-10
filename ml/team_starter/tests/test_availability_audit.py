"""Synthetic fixtures only; these are not native incident evidence."""
import json
from contextlib import closing
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ml.team_starter.availability_audit import audit, read_journal, load_archive


def snapshot(cutoff=1000., observed="1970-01-01T00:16:35+00:00"):
    return {
        "contract_version": "adaptivenet.features.v1", "snapshot_id": "features-test-001",
        "computed_at_unix": cutoff,
        "regions": {"region-a": {"features": {"app_up": 1., "http_p95_ms": None},
                                "provenance": {"app_up": {"source": "aws_amp", "observed_at": observed},
                                               "ripe_atlas": {"source": "ripe_atlas", "latest_observed_at": 980}}}},
        "cloudfront": {"provenance": {"cloudfront_requests": {
            "source": "aws_cloudwatch", "observed_at": 900}}},
    }


class AvailabilityAuditTests(unittest.TestCase):
    def test_matching_snapshot_and_run_with_missing_value(self):
        report = audit([snapshot()], {"features-test-001": 1000.}, [1000.], {"ok": 1})
        self.assertEqual(report["status"], "limited_availability_evidence")
        self.assertEqual(report["counts"]["null_feature_values"], 1)
        self.assertEqual(report["counts"]["matched_current_ingestion_runs"], 1)
        self.assertFalse(report["strict_historical_raw_replay_certified"])

    def test_future_dated_provenance_rejected(self):
        snap = snapshot(observed="1970-01-01T00:16:41+00:00")
        report = audit([snap], {"features-test-001": 1000.}, [1000.], {"ok": 1})
        self.assertEqual(report["status"], "issues_found")
        self.assertEqual(report["counts"]["future_dated_source_entries"], 1)

    def test_duplicate_snapshots_rejected(self):
        snap = snapshot()
        report = audit([snap, snap], {"features-test-001": 1000.}, [1000.], {})
        self.assertEqual(report["status"], "issues_found")
        self.assertIn("duplicate_archived_snapshot", [i["type"] for i in report["issues"]])

    def test_old_archive_not_in_pruned_database_is_limitation_not_fraud(self):
        report = audit([snapshot()], {}, [], {})
        self.assertEqual(report["status"], "limited_availability_evidence")
        self.assertEqual(report["counts"]["not_in_current_sqlite"], 1)

    def test_real_sqlite_read_only(self):
        with TemporaryDirectory() as d:
            db = Path(d) / "test.db"
            # Explicit close is necessary on Windows. The sqlite3 connection
            # context manager commits/rolls back but does not close the handle.
            with closing(sqlite3.connect(db)) as con:
                con.execute("CREATE TABLE feature_snapshots (snapshot_id TEXT, computed_at REAL)")
                con.execute("CREATE TABLE ingestion_runs (collected_at REAL, source_status TEXT, errors_json TEXT)")
                con.execute("INSERT INTO feature_snapshots VALUES (?,?)", ("features-test-001", 1000.))
                con.execute("INSERT INTO ingestion_runs VALUES (?,?,?)", (1000., "ok", "[]"))
                con.commit()
            snaps, run_times, statuses = read_journal(db)
            self.assertEqual(snaps["features-test-001"], 1000.)
            self.assertEqual(run_times, [1000.])
            self.assertEqual(statuses["ok"], 1)

    def test_archive_read(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "test.jsonl"
            path.write_text(json.dumps(snapshot()) + "\n", encoding="utf-8")
            self.assertEqual(load_archive(path)[0]["snapshot_id"], "features-test-001")


if __name__ == "__main__":
    unittest.main()
