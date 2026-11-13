"""Trained fixture model -> safety reviewer -> EKS decision (dry run, no AWS)."""
from __future__ import annotations
from pathlib import Path
import tempfile
import time
import unittest

try:
    import sklearn  # noqa: F401
except ImportError:  # optional '.[ml]' dependency
    sklearn = None

from adaptivenet.controller.end_to_end import decide
from adaptivenet.ingestion.handoff import build_model_input
from ml.team_starter.dataset import make_dataset
from ml.team_starter.shadow import shadow_predict
from ml.team_starter.training import train_and_evaluate
from ml.team_starter.tests.test_offline_foundation import fake_snapshot, manifest, reviewed


@unittest.skipIf(sklearn is None, "install optional package: python -m pip install -e '.[ml]'")
class ModelToDecisionTests(unittest.TestCase):
    def test_model_proposal_reaches_a_dry_run_eks_decision(self):
        # Synthetic fixture only; not a performance claim.
        t = time.time() - 100
        labels = ["healthy", "network_degradation"] * 4
        snapshots, episodes = [], []
        for i, label in enumerate(labels):
            start = t - 30_000 + i * 3000
            episodes.append(reviewed(f"synthetic-fixture-{i}", label, start, start + 700))
            for j in range(4):
                snapshots.append(fake_snapshot(start + 50 + j * 100, cpu=5 if label == "healthy" else 70,
                                               loss=0 if label == "healthy" else 80))
        with tempfile.TemporaryDirectory() as dirname:
            result = train_and_evaluate(make_dataset(snapshots, [], manifest(episodes)),
                                        Path(dirname), group="aws_ripe")
            self.assertEqual(result["status"], "trained", result.get("blockers"))
            fresh = fake_snapshot(time.time() - 15, cpu=70, loss=80)
            proposal = shadow_predict(build_model_input(fresh, mode="combined", now=time.time()),
                                      Path(result["model_artifact"]))
        decision = decide(fresh, proposal)
        self.assertEqual(decision["snapshot_id"], proposal["snapshot_id"])
        self.assertIn(decision["decision"], ("scale", "failover", "no-op"))
        self.assertFalse(decision["executed"])
        self.assertTrue(decision["dry_run"])
        self.assertFalse(decision["review"].get("approved_for_execution", False))


if __name__ == "__main__":
    unittest.main()
