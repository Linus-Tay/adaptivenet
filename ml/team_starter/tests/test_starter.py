"""Offline contract tests. Synthetic inputs are TEST FIXTURES, never training rows."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ml.team_starter.archive import archive
from ml.team_starter.feature_views import AWS_COLUMNS, RIPE_COLUMNS, project
from adaptivenet.ingestion.handoff import conservative_rule_baseline


def synthetic_input():
    cols = AWS_COLUMNS + RIPE_COLUMNS
    vals = {c: 1 if c == "app_up" else 0.5 for c in cols}
    vals.update({"memory_available_bytes": 70, "memory_total_bytes": 100,
                 "memory_available_pct": 70, "cpu_busy_pct": 1})
    return {
        "contract_version": "adaptivenet.model_input.v1",
        "snapshot_id": "features-synthetic-test-only",
        "ready_for_inference": True,
        "features": {"region-a": dict(vals), "region-b": dict(vals)},
        "comparison": {"rtt_p50_a_minus_b_ms": 99},
        "cloudfront": {"last_known": {"cloudfront_requests": 999}},
        "quality": {"region-a": {"external": "fresh"}},
        "actions_enabled": False,
    }


def synthetic_detection():
    s = synthetic_input()
    scopes = {}
    for scope in ("cloudfront", "region-a", "region-b"):
        scopes[scope] = {
            "consecutive_failed_requests": 0,
            "windows": {w: {"failure_ratio": 0.0,
                             "http_latency_p95_ms": 20.0,
                             "count": 12}
                        for w in ("60s", "300s", "900s")}
        }
    return {"contract_version": "adaptivenet.detection_input.v1",
            "slow": s, "fast": {"contract_version": "adaptivenet.fast_signals.v1",
                                "ready_for_fast_assessment": True, "scopes": scopes}}


class StarterContractTests(unittest.TestCase):
    def test_aws_arm_has_no_external_leakage(self):
        output = project(synthetic_input(), "aws_only")
        self.assertEqual(len(output["values"]), 26)
        self.assertFalse(any("ripe" in k or "network_rtt" in k or "packet_loss" in k
                             or "http.cloudfront" in k for k in output["values"]))
        self.assertFalse(any("comparison" in k or "last_known" in k for k in output["values"]))
        self.assertEqual(set(output["values"]), set(output["missing"]))

    def test_ripe_only_in_combined_arm(self):
        output = project(synthetic_input(), "aws_ripe")
        self.assertEqual(len(output["values"]), 34)
        self.assertIn("region-b.network_rtt_p50_ms", output["values"])

    def test_http_requires_fast_contract(self):
        with self.assertRaises(ValueError):
            project(synthetic_input(), "aws_ripe_http")
        output = project(synthetic_detection(), "aws_ripe_http")
        self.assertTrue(output["ready_for_shadow_assessment"])
        self.assertIn("http.region-a.60s.failure_ratio", output["values"])
        self.assertEqual(len(output["values"]), 64)

    def test_missing_stays_missing_and_is_masked(self):
        item = synthetic_input()
        item["features"]["region-a"]["http_p95_ms"] = None
        view = project(item, "aws_only")
        self.assertIsNone(view["values"]["region-a.http_p95_ms"])
        self.assertTrue(view["missing"]["region-a.http_p95_ms"])

    def test_baseline_never_requests_aws_writes(self):
        result = conservative_rule_baseline(synthetic_input())
        self.assertIn(result["action"], ("hold", "investigate"))
        self.assertEqual(result["contract_version"], "adaptivenet.proposal.v1")

    def test_archive_is_idempotent_and_preserves_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "source.db"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE feature_snapshots (snapshot_id TEXT PRIMARY KEY, computed_at REAL, body_json TEXT)")
            snap = {"snapshot_id": "features-test", "computed_at_unix": 10.0}
            con.execute("INSERT INTO feature_snapshots VALUES (?,?,?)",
                        (snap["snapshot_id"], 10, json.dumps(snap)))
            con.commit(); con.close()
            witness = root / "probe.jsonl"
            witness.write_text(json.dumps({"timestamp":"2026-01-01T00:00:00Z","scope":"region-a", "outcome":"ok"}) + "\n", encoding="utf-8")
            out = root / "output"
            self.assertEqual(archive(db, witness, out), (1, 1))
            self.assertEqual(archive(db, witness, out), (0, 0))
            self.assertEqual(json.loads((out / "feature_snapshots.jsonl").read_text(encoding="utf-8").strip()), snap)
            self.assertTrue(db.exists())


if __name__ == "__main__":
    unittest.main()
