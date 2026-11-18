"""SYNTHETIC TEST FIXTURES ONLY: checks the analysis runs off the recording Mac."""
import json

from ml.team_starter import live_shadow as ls
from ml.team_starter.ripe_value import campaign_slots


def test_missing_absolute_trial_path_falls_back_to_trial_dir(tmp_path):
    trials = tmp_path / "trials"
    trials.mkdir()
    (trials / "slot-0.json").write_text(json.dumps({
        "requested_utc": "2026-11-12T15:22:00+00:00", "region": "region-b",
        "configured_packet_loss_percent": 35, "duration_seconds": 960,
        "remote_rollback_armed_and_rules_added": True}))
    plan = {"slots": [{"slot": 0, "control_region": "region-a"}]}
    log = [{"slot": 0, "status": "submitted",
            "trial_record": "/Users/someone-else/campaign/trials/slot-0.json"}]
    [slot] = campaign_slots(plan, log, trials)
    assert slot["region"] == "region-b" and slot["effect_confirmed"]


def test_retrain_does_not_commit_unless_asked():
    base = ["retrain", "--campaign-dir", "c", "--live-dir", "l"]
    assert ls._parser().parse_args(base).commit is False
    assert ls._parser().parse_args(base + ["--commit"]).commit is True
