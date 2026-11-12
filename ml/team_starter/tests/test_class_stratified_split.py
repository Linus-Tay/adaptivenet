"""Class-stratified split and unlabeled window replay. FIXTURES ONLY, not evidence."""
import unittest

from ml.team_starter.dataset import make_dataset, unlabeled_window_rows
from ml.team_starter.evaluation import split_by, split_episodes_class_stratified
from ml.team_starter.tests.test_offline_foundation import fake_snapshot, manifest, reviewed
from ml.team_starter.verified_labels import validate_manifest

T0 = 1_790_000_000
H = 3600


def block_design():
    """All app failures on day 1, all network trials on day 2 (like the real study)."""
    items = []
    for i in range(4):
        items.append(reviewed(f"app-{i}", "application_failure", T0 + i * 2 * H, T0 + i * 2 * H + 300))
    for i in range(3):
        s = T0 + 10 * H + i * 2 * H
        items.append(reviewed(f"ok-{i}", "healthy", s, s + 1200, region="both"))
    for i in range(4):
        s = T0 + 30 * H + i * 2 * H
        items.append(reviewed(f"net-{i}", "network_degradation", s, s + 1200, region="region-b"))
    return validate_manifest(manifest(items))


class ClassStratifiedSplitTests(unittest.TestCase):
    def test_latest_run_of_each_class_is_test(self):
        parts = split_episodes_class_stratified(block_design())
        self.assertEqual(sorted(parts["test"]), ["app-3", "net-3", "ok-2"])
        self.assertEqual(sorted(parts["validation"]), ["app-2", "net-2"])  # only classes with >=4 runs
        self.assertIn("ok-0", parts["train"])
        self.assertEqual(parts["strategy"], "class_stratified")

    def test_chronological_split_puts_each_class_in_one_partition(self):
        parts = split_by("chronological", block_design())
        self.assertFalse(any(i.startswith("app") for i in parts["test"]))

    def test_embargo_drops_train_run_adjacent_to_test(self):
        eps = block_design()
        # Move training healthy run ok-1 to end 5 minutes before held-out app-3 starts.
        for e in eps:
            if e["episode_id"] == "ok-1":
                e["start_unix"], e["end_unix"] = T0 + 6 * H - 1500, T0 + 6 * H - 300
        parts = split_episodes_class_stratified(eps, embargo_seconds=900)
        self.assertIn("app-3", parts["test"])
        self.assertIn("ok-1", parts["excluded_for_embargo"])
        self.assertNotIn("ok-1", parts["train"])

    def test_phases_of_one_run_stay_together(self):
        items = [reviewed("a-fail", "application_failure", T0, T0 + 300, run_id="r1"),
                 reviewed("a-rec", "recovery", T0 + 300, T0 + 600, run_id="r1"),
                 reviewed("b-fail", "application_failure", T0 + 5 * H, T0 + 5 * H + 300, run_id="r2")]
        parts = split_episodes_class_stratified(validate_manifest(manifest(items)))
        self.assertEqual(sorted(parts["train"]), ["a-fail", "a-rec"])
        self.assertEqual(parts["test"], ["b-fail"])

    def test_class_with_single_run_is_rejected(self):
        items = [reviewed("x", "healthy", T0, T0 + 600), reviewed("y", "healthy", T0 + 5 * H, T0 + 5 * H + 600),
                 reviewed("z", "network_degradation", T0 + 9 * H, T0 + 9 * H + 600)]
        with self.assertRaises(ValueError):
            split_episodes_class_stratified(validate_manifest(manifest(items)))

    def test_unknown_strategy_rejected(self):
        with self.assertRaises(ValueError):
            split_by("random", block_design())


class UnlabeledWindowTests(unittest.TestCase):
    def test_rows_have_no_label_and_respect_window(self):
        snaps = [fake_snapshot(T0 + k * 60) for k in range(10)]
        rows = unlabeled_window_rows(snaps, [], T0 + 120, T0 + 300)
        self.assertEqual([r["at_unix"] for r in rows], [T0 + 120, T0 + 180, T0 + 240])
        self.assertTrue(all(r["label"] is None and r["unreviewed"] for r in rows))

    def test_views_match_labeled_dataset_builder(self):
        snaps = [fake_snapshot(T0 + k * 60) for k in range(5)]
        labeled = make_dataset(snaps, [], manifest([reviewed("e", "healthy", T0, T0 + 600)]))["rows"]
        unlabeled = unlabeled_window_rows(snaps, [], T0, T0 + 600)
        self.assertEqual([r["views"] for r in labeled], [r["views"] for r in unlabeled])


if __name__ == "__main__":
    unittest.main()
