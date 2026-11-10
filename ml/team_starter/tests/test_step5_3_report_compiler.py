"""Report unit tests use explicitly invented synthetic fixture data; no real labels."""
import json
import tempfile
import unittest
from pathlib import Path

from ml.team_starter.report_compiler import GROUPS, compile_report, main


def synthetic_audit():
    return {
        "status": "limited_availability_evidence",
        "counts": {
            "archive_snapshots": 4,
            "matched_current_ingestion_runs": 4,
            "matched_current_sqlite_snapshots": 4,
            "timestamped_source_provenance_entries": 7,
            "null_feature_values": 2,
        },
        "journal_status_counts": {"ok": 3, "partial": 1},
        "total_issue_count": 0,
        "strict_historical_raw_replay_certified": False,
    }


def synthetic_eval(trained=False):
    return {g: ({"status": "trained", "model_artifact": "synthetic-local-model.pkl",
                 "test": {"status": "evaluated", "samples": 10, "macro_f1": .75,
                          "abstentions": 2}}
                if trained else {"status": "not_trained", "model_artifact": None,
                                 "blockers": ["no_independently_verified_labeled_snapshots"]})
            for g in GROUPS}


def synthetic_paired(evaluated=False):
    if not evaluated:
        return {"status": "not_evaluable", "blockers": ["missing_trained_native_held_out_predictions"]}
    return {"status": "evaluated", "report_version": "adaptivenet.paired_incident_evaluation.v1",
            "verified_held_out_incidents": 2, "paired": {}}


def synthetic_shadow():
    return {"proposal": {"model_id": "conservative_rule_baseline_v1", "action": "hold"},
            "decision_review": {"policy_valid": True, "executed": False,
                                "approved_for_execution": False, "effective_action": "hold"}}


class ReportCompilerTests(unittest.TestCase):
    def test_native_without_labels_remains_not_evaluable(self):
        text = compile_report(synthetic_audit(), synthetic_eval(), synthetic_paired(),
                              synthetic_shadow(), created_at="synthetic")
        self.assertIn("NOT EVALUABLE", text)
        self.assertNotIn("0.7500", text)
        self.assertIn("Strict historical raw replay is not certified", text)
        self.assertIn("conservative_rule_baseline_v1", text)

    def test_missing_artifacts_stay_missing(self):
        text = compile_report(None, None, None, None, created_at="synthetic")
        self.assertIn("No availability-audit artifact supplied", text)
        self.assertIn("report missing", text)
        self.assertIn("NOT EVALUABLE", text)

    def test_factual_count_figure_but_no_performance_figure_when_untrained(self):
        with tempfile.TemporaryDirectory() as folder:
            figures = Path(folder) / "figures"
            text = compile_report(synthetic_audit(), synthetic_eval(), synthetic_paired(),
                                  synthetic_shadow(), created_at="synthetic",
                                  figures_dir=figures, out_dir=Path(folder))
            self.assertTrue((figures / "ingestion-status-counts.svg").exists())
            self.assertFalse((figures / "native-heldout-macro-f1.svg").exists())
            self.assertIn("ingestion-status-counts.svg", text)

    def test_three_trained_with_paired_evidence_can_show_score_chart(self):
        with tempfile.TemporaryDirectory() as folder:
            figures = Path(folder) / "figures"
            text = compile_report(synthetic_audit(), synthetic_eval(True), synthetic_paired(True),
                                  synthetic_shadow(), created_at="synthetic",
                                  figures_dir=figures, out_dir=Path(folder))
            self.assertTrue((figures / "native-heldout-macro-f1.svg").exists())
            self.assertIn("0.7500", text)
            self.assertIn("Paired evaluator: `evaluated`", text)

    def test_paired_claim_without_all_trained_is_blocked(self):
        text = compile_report(synthetic_audit(), synthetic_eval(False), synthetic_paired(True),
                              synthetic_shadow(), created_at="synthetic")
        self.assertIn("NOT EVALUABLE", text)
        self.assertNotIn("Paired evaluator: `evaluated`", text)

    def test_invalid_counter_rejected(self):
        audit = synthetic_audit()
        audit["counts"]["archive_snapshots"] = -1
        with self.assertRaisesRegex(ValueError, "Invalid count"):
            compile_report(audit, {}, {}, {}, created_at="synthetic")

    def test_execution_flagged_in_report(self):
        shadow = synthetic_shadow()
        shadow["decision_review"]["executed"] = True
        text = compile_report(None, None, None, shadow, created_at="synthetic")
        self.assertIn("SAFETY WARNING", text)

    def test_cli_writes_report_and_no_native_chart_when_no_labels(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fixtures = {"audit.json": synthetic_audit(),
                        "evaluation.json": synthetic_eval(),
                        "paired.json": synthetic_paired(),
                        "shadow.json": synthetic_shadow()}
            for name, obj in fixtures.items():
                (root / name).write_text(json.dumps(obj), encoding="utf-8")
            out = root / "report.md"
            status = main(["--audit", str(root / "audit.json"),
                           "--evaluation", str(root / "evaluation.json"),
                           "--paired", str(root / "paired.json"),
                           "--shadow", str(root / "shadow.json"),
                           "--out", str(out)])
            self.assertEqual(status, 0)
            self.assertIn("NOT EVALUABLE", out.read_text(encoding="utf-8"))
            self.assertFalse((root / "figures" / "native-heldout-macro-f1.svg").exists())

    def test_input_evidence_file_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            p = root / "data.json"
            p.write_text("{}", encoding="utf-8")
            with self.assertRaises(SystemExit):
                main(["--audit", str(p), "--evaluation", str(p),
                      "--paired", str(p), "--shadow", str(p),
                      "--out", str(p)])
            self.assertEqual(p.read_text(encoding="utf-8"), "{}")


if __name__ == "__main__":
    unittest.main()
