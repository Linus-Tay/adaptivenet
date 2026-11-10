"""The simulated comparison is deterministic and offline; these pin its qualitative claims."""
import importlib.util
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("pid_comparison", ROOT / "experiments/pid_comparison.py")
sim = importlib.util.module_from_spec(spec)
sys.modules["pid_comparison"] = sim
spec.loader.exec_module(sim)

SEEDS = 4
scenario = lambda name: next(s for s in sim.SCENARIOS if s.name == name)
mean = lambda sc, make: sim.aggregate([sim.run(scenario(sc), make(), seed) for seed in range(SEEDS)])


def test_simulation_is_deterministic_per_seed():
    a = sim.run(scenario("capacity_loss"), sim.PID(), 3)
    b = sim.run(scenario("capacity_loss"), sim.PID(), 3)
    assert a == b


def test_simulator_imports_no_network_or_cloud_clients():
    source = (ROOT / "experiments/pid_comparison.py").read_text()
    for forbidden in ("import httpx", "import boto3", "import requests", "import socket", "urllib", "subprocess"):
        assert forbidden not in source


def test_pid_finds_a_split_where_the_threshold_rule_limit_cycles():
    threshold, pid = mean("capacity_loss", sim.ThresholdRule), mean("capacity_loss", sim.PID)
    assert pid["bad_request_pct"] < 10 < threshold["bad_request_pct"]
    assert pid["reversals"] <= 2 < threshold["reversals"]
    assert 15 <= pid["final_weight_a_median"] <= 39   # the feasible band for this plant


def test_pid_drains_a_sustained_fault_further_than_the_threshold_rule():
    threshold, pid = mean("sustained_latency", sim.ThresholdRule), mean("sustained_latency", sim.PID)
    assert pid["bad_request_pct"] < threshold["bad_request_pct"]
    assert pid["final_weight_a_median"] <= 10


def test_no_controller_moves_on_unknown_telemetry():
    for name in ("telemetry_gap", "null_p95", "drain_blind_spot"):
        for make in (sim.ThresholdRule, sim.PID):
            assert mean(name, make)["moves_while_unknown"] == 0
    assert mean("null_p95", sim.PID)["weight_changes"] == 0


def test_threshold_engine_raises_on_null_p95_with_low_error_ratio():
    """Documents a latent defect in controller/engine.py that the comparison surfaced."""
    assert mean("null_p95", sim.ThresholdRule)["controller_errors"] > 0


def test_both_degraded_does_not_cause_meaningful_shifting():
    assert mean("both_degraded", sim.PID)["travel_pp"] <= 5


def test_excessive_gain_oscillates_which_is_why_gains_are_bounded():
    base = sim.PIDConfig()
    hot = lambda: sim.PID(sim.PIDConfig(kp=base.kp * 8, ki=base.ki * 8))
    assert mean("capacity_loss", hot)["reversals"] > 10 * max(1, mean("capacity_loss", sim.PID)["reversals"])


def test_ml_advisor_is_optional_and_null_inputs_give_no_advice(tmp_path):
    advisor, info = sim.load_advisor(tmp_path / "missing.jsonl")
    assert advisor is None and info["available"] is False
    rows = [{"split": "train", "label": label, "int_p95_ms": i, "int_error_ratio": 0.0, "ext_p95_ms": e, "ext_error_ratio": 0.0}
            for label, i, e in (("healthy", 30, 30), ("network", 30, 1500), ("compute", 1500, 1500)) for _ in range(40)]
    knn = sim.KnnAdvisor(rows, stride=1)
    assert knn.classify(32, 0.0, 1400, 0.0)["label"] == "network"
    assert knn.classify(None, 0.0, 1400, 0.0) is None
    assert knn({"status": "stale"}, {"status": "ok", "p95_ms": 30, "error_ratio": 0}) is None
