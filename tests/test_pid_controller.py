"""Offline tests for the shadow-only PID weight controller. No network, gateway or AWS."""
import ast
import math
from pathlib import Path
import time

import pytest

from adaptivenet.controller import pid as pid_module
from adaptivenet.controller.pid import (PIDConfig, PIDWeightController, advisor_gain_scale,
                                        observations_from_model_input, region_pressure, to_proposal)
from adaptivenet.ingestion.decisions import review_proposal

GOOD = {"status": "ok", "age_seconds": 0, "samples": 5, "error_ratio": 0, "p95_ms": 40}
SLOW = {"status": "ok", "age_seconds": 0, "samples": 5, "error_ratio": 0, "p95_ms": 1500}
ERRORS = {"status": "ok", "age_seconds": 0, "samples": 5, "error_ratio": .5, "p95_ms": 40}


def drive(controller, a, b, weight, samples, start=0.0, interval=15.0, apply=True):
    """Closed loop when apply=True (the proposal becomes the weight), shadow otherwise."""
    history = []
    for i in range(samples):
        decision = controller.step(a, b, weight, start + i * interval)
        history.append(decision)
        if apply:
            weight = decision["target_region_a_percent"]
    return weight, history


# ---- pressure: null is unknown, never zero ---------------------------------------
@pytest.mark.parametrize("obs", [
    None, {}, {"status": "stale"}, {"status": "not_configured"},
    {**GOOD, "age_seconds": 91}, {**GOOD, "age_seconds": -1}, {**GOOD, "age_seconds": None},
    {**GOOD, "samples": 2}, {**GOOD, "p95_ms": None}, {**GOOD, "error_ratio": None},
    {**GOOD, "p95_ms": float("nan")}, {**GOOD, "p95_ms": float("inf")}, {**GOOD, "p95_ms": -5},
    {**GOOD, "error_ratio": 1.5}, {**GOOD, "p95_ms": True}, {**GOOD, "p95_ms": None, "error_ratio": None},
])
def test_missing_stale_or_invalid_observation_is_unknown(obs):
    assert region_pressure(obs, PIDConfig()) is None


def test_a_null_signal_is_only_ignored_when_the_other_already_proves_a_breach():
    cfg = PIDConfig()
    assert region_pressure({**GOOD, "p95_ms": None, "error_ratio": 1.0}, cfg) == pytest.approx(20)  # all requests failed
    assert region_pressure({**SLOW, "error_ratio": None}, cfg) == pytest.approx(3.75)
    assert region_pressure(GOOD, cfg) == pytest.approx(0.1)


# ---- behaviour ---------------------------------------------------------------------
def test_healthy_regions_never_move_the_weight():
    weight, history = drive(PIDWeightController(), GOOD, GOOD, 50, 40)
    assert weight == 50 and all(d["action"] == "hold" for d in history)


@pytest.mark.parametrize("bad", [SLOW, ERRORS])
def test_sustained_breach_drains_the_bad_region_and_stops_at_the_clamp(bad):
    weight, history = drive(PIDWeightController(), bad, GOOD, 100, 20)
    assert weight == 0
    assert all(0 <= d["target_region_a_percent"] <= 100 for d in history)
    weight, _ = drive(PIDWeightController(), GOOD, bad, 0, 20)
    assert weight == 100


def test_output_clamp_is_configurable():
    weight, _ = drive(PIDWeightController(PIDConfig(min_weight=10, max_weight=90)), SLOW, GOOD, 50, 30)
    assert weight == 10
    weight, _ = drive(PIDWeightController(PIDConfig(min_weight=10, max_weight=90)), GOOD, SLOW, 50, 30)
    assert weight == 90


def test_slew_limit_never_exceeds_the_25_point_review_limit():
    huge = PIDConfig(kp=500, ki=500)
    weight, previous = 100, 100
    controller = PIDWeightController(huge)
    for i in range(8):
        decision = controller.step(SLOW, GOOD, weight, i * 15.0)
        weight = decision["target_region_a_percent"]
        assert abs(weight - previous) <= 25
        previous = weight
    assert weight == 0
    with pytest.raises(ValueError):
        PIDConfig(max_step_pct=30)


def test_anti_windup_recovers_immediately_after_long_saturation():
    """200 saturated samples must not bank integral that delays the reversal."""
    controller = PIDWeightController()
    weight, _ = drive(controller, SLOW, GOOD, 100, 200)
    assert weight == 0
    weight, history = drive(controller, GOOD, SLOW, weight, 3, start=200 * 15.0)
    assert history[0]["target_region_a_percent"] > 0 or history[1]["target_region_a_percent"] > 0
    assert weight > 0


def test_shadow_controller_cannot_wind_up_when_nothing_is_applied():
    controller = PIDWeightController()
    _, history = drive(controller, SLOW, GOOD, 100, 100, apply=False)
    assert min(d["target_region_a_percent"] for d in history) >= 75   # always one bounded step from reality
    after = controller.step(GOOD, GOOD, 100, 100 * 15.0)
    assert after["target_region_a_percent"] >= 100 - 1


def test_operator_override_is_tracked_bumplessly():
    controller = PIDWeightController()
    weight, _ = drive(controller, SLOW, GOOD, 100, 3)
    assert weight < 100
    decision = controller.step(GOOD, GOOD, 80, 3 * 15.0)   # operator moved it to 80 meanwhile
    assert abs(decision["target_region_a_percent"] - 80) <= 25


def test_slow_sample_interval_holds_between_samples():
    controller = PIDWeightController(PIDConfig(sample_interval_s=30))
    first = controller.step(SLOW, GOOD, 100, 0.0)
    assert first["action"] == "propose_shift"
    weight = first["target_region_a_percent"]
    for t in (1, 5, 15, 29.9):
        held = controller.step(SLOW, GOOD, weight, t)
        assert held["action"] == "hold" and held["reason"] == "between_samples" and held["target_region_a_percent"] == weight
    assert controller.step(SLOW, GOOD, weight, 30.0)["action"] == "propose_shift"


def test_one_slow_probe_inside_a_sample_window_is_rejected_by_the_median():
    controller = PIDWeightController()
    controller.step(GOOD, GOOD, 50, 0.0)
    weight = 50
    for sample in range(1, 20):
        base = sample * 15.0
        controller.step(SLOW, GOOD, weight, base - 10)   # one spike
        controller.step(GOOD, GOOD, weight, base - 5)
        decision = controller.step(GOOD, GOOD, weight, base)
        assert decision["action"] == "hold" and decision["target_region_a_percent"] == 50


@pytest.mark.parametrize("unknown", [None, {"status": "stale"}, {**GOOD, "age_seconds": 500},
                                     {**GOOD, "p95_ms": None}, {**GOOD, "samples": 1}])
def test_unknown_data_holds_last_output_and_never_reads_as_zero(unknown):
    controller = PIDWeightController()
    weight, _ = drive(controller, SLOW, GOOD, 100, 2)
    assert 0 < weight < 100
    for i in range(2, 60):   # a long outage of region-a telemetry while the fault continues
        decision = controller.step(unknown, GOOD, weight, i * 15.0)
        assert decision["action"] == "hold" and decision["reason"].startswith("unknown_telemetry:region-a")
        assert decision["target_region_a_percent"] == weight
    for i in range(60, 120):  # the healthy side going dark must not look like "region-b is fine" either
        assert controller.step(SLOW, unknown, weight, i * 15.0)["target_region_a_percent"] == weight
    # data returns after a 30 minute gap: one bounded step, not 30 minutes of banked integral
    resumed = controller.step(SLOW, GOOD, weight, 120 * 15.0)
    assert 0 < weight - resumed["target_region_a_percent"] <= 25
    assert resumed["dt_s"] <= 2 * PIDConfig().sample_interval_s


def test_both_regions_equally_degraded_do_not_shift():
    weight, history = drive(PIDWeightController(), SLOW, SLOW, 50, 30)
    assert weight == 50 and all(d["action"] == "hold" for d in history)


def test_no_automatic_failback_by_default_and_gated_failback_when_enabled():
    controller = PIDWeightController()
    weight, _ = drive(controller, SLOW, GOOD, 100, 20)
    weight, _ = drive(controller, GOOD, GOOD, weight, 100, start=20 * 15.0)
    assert weight <= 10   # only the proportional release when the fault clears, then it stays

    controller = PIDWeightController(PIDConfig(failback_rate_pct_per_s=0.2, home_weight=100, failback_after_s=120))
    weight, _ = drive(controller, SLOW, GOOD, 100, 20)
    low = weight
    weight, history = drive(controller, GOOD, GOOD, weight, 8, start=20 * 15.0)
    assert weight <= low + 10          # still inside the 120 s healthy wait
    weight, history = drive(controller, GOOD, GOOD, weight, 60, start=28 * 15.0)
    assert weight == 100 and any(d["reason"] == "failback_drift" for d in history)
    steps = [abs(b["target_region_a_percent"] - a["target_region_a_percent"]) for a, b in zip(history, history[1:])]
    assert max(steps) <= 4             # 0.2 pp/s * 15 s = 3 pp per sample (plus rounding)


def test_failback_does_not_run_while_a_region_is_unknown():
    controller = PIDWeightController(PIDConfig(failback_rate_pct_per_s=1.0, home_weight=100, failback_after_s=0))
    weight, _ = drive(controller, SLOW, GOOD, 100, 20)
    weight, _ = drive(controller, {"status": "stale"}, GOOD, weight, 50, start=20 * 15.0)
    assert weight == 0


def test_clock_regression_and_bad_inputs_are_safe():
    controller = PIDWeightController()
    controller.step(SLOW, GOOD, 100, 1000.0)
    assert controller.step(SLOW, GOOD, 75, 10.0)["reason"] == "clock_regressed"
    for bad in (True, 101, -1, 50.0, None):
        with pytest.raises(ValueError):
            controller.step(GOOD, GOOD, bad, 2000.0)
    with pytest.raises(ValueError):
        controller.step(GOOD, GOOD, 50, float("nan"))


def test_derivative_term_is_finite_and_optional():
    controller = PIDWeightController(PIDConfig(kd=20))
    weight, history = drive(controller, SLOW, GOOD, 100, 20)
    assert weight == 0 and all(math.isfinite(d["terms"]["d"]) for d in history if "terms" in d)


# ---- advisor (ML-guided) gain schedule -----------------------------------------------
H, N, C, X = ({"label": l, "confidence": .9} for l in ("healthy", "network", "compute", "critical"))

@pytest.mark.parametrize("a,b,expected", [
    (N, H, 1.9), (H, N, 1.9), (X, H, 1.9), (C, H, 1.0), (H, H, 0.5), (N, C, 0.0), (X, X, 0.0),
    (None, H, 1.0), ({"label": "network", "confidence": .3}, H, 1.0), ({"label": "banana", "confidence": 1}, H, 1.0),
    ({"label": "network", "confidence": float("nan")}, H, 1.0), ({"label": "network", "confidence": 7}, H, 1.0),
])
def test_advisor_gain_scale_is_bounded_and_fails_to_plain_pid(a, b, expected):
    assert advisor_gain_scale(a, b) == pytest.approx(expected)


def test_advisor_can_freeze_or_speed_up_but_never_break_the_limits():
    frozen = PIDWeightController()
    weight, history = drive(frozen, SLOW, GOOD, 100, 1)
    held = frozen.step(SLOW, GOOD, weight, 15.0, gain_scale=0)
    assert held["action"] == "hold" and held["reason"] == "advisor_freeze"

    plain, boosted = PIDWeightController(), PIDWeightController()
    p = plain.step(SLOW, GOOD, 100, 0.0)["target_region_a_percent"]
    q = boosted.step(SLOW, GOOD, 100, 0.0, gain_scale=1e9)["target_region_a_percent"]   # absurd advice
    assert 75 <= q <= p
    # an advisor cannot make the controller act on unknown data, and garbage advice is ignored
    assert PIDWeightController().step(None, GOOD, 100, 0.0, gain_scale=2)["action"] == "hold"
    assert PIDWeightController().step(SLOW, GOOD, 100, 0.0, gain_scale=float("nan"))["target_region_a_percent"] == p
    assert PIDWeightController().step(SLOW, GOOD, 100, 0.0, gain_scale=-3)["target_region_a_percent"] == p


# ---- integration with the existing shadow contracts ----------------------------------
def snapshot(now, **a_features):
    region = lambda extra: {"features": {"app_up": 1, "memory_available_pct": 60, "cpu_busy_pct": 20,
                                         "http_p95_ms": 40, "http_error_ratio": 0.0, **extra},
                            "quality": {"internal": "fresh", "external": "fresh"}}
    return {"contract_version": "adaptivenet.features.v1", "snapshot_id": "features-test", "computed_at_unix": now,
            "ready_for_model": True, "ready_for_internal_only_model": True,
            "regions": {"region-a": region(a_features), "region-b": region({})}}


def model_input(snap, ready=True, age=5):
    return {"ready_for_inference": ready, "age_seconds": age,
            "features": {r: snap["regions"][r]["features"] for r in snap["regions"]},
            "quality": {r: snap["regions"][r]["quality"] for r in snap["regions"]}}


def test_pid_proposal_passes_review_but_is_never_executed():
    snap = snapshot(time.time(), http_p95_ms=1500)
    obs = observations_from_model_input(model_input(snap))
    decision = PIDWeightController().step(obs["region-a"], obs["region-b"], 100, 0.0)
    assert decision["action"] == "propose_shift" and decision["shadow_only"] is True
    review = review_proposal(snap, to_proposal(decision, snap["snapshot_id"]), current_region_a_percent=100)
    assert review["policy_valid"] is True and review["eligible_for_operator_review"] is True
    assert review["approved_for_execution"] is False and review["executed"] is False and review["effective_action"] == "hold"


def test_model_input_gates_are_respected():
    snap = snapshot(time.time(), http_p95_ms=1500)
    not_ready = observations_from_model_input(model_input(snap, ready=False))
    assert PIDWeightController().step(not_ready["region-a"], not_ready["region-b"], 100, 0.0)["action"] == "hold"
    old = observations_from_model_input(model_input(snap, age=600))
    assert PIDWeightController().step(old["region-a"], old["region-b"], 100, 0.0)["action"] == "hold"
    snap["regions"]["region-b"]["quality"]["internal"] = "missing"
    missing = observations_from_model_input(model_input(snap))
    assert PIDWeightController().step(missing["region-a"], missing["region-b"], 100, 0.0)["action"] == "hold"
    null = observations_from_model_input(model_input(snapshot(time.time(), http_p95_ms=None)))
    assert PIDWeightController().step(null["region-a"], null["region-b"], 100, 0.0)["action"] == "hold"
    held = PIDWeightController().step(null["region-a"], null["region-b"], 100, 0.0)
    assert to_proposal(held, "features-test")["action"] == "hold" and "target_region_a_percent" not in to_proposal(held, "features-test")


def test_module_is_pure_no_network_aws_or_actuator_imports():
    tree = ast.parse(Path(pid_module.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0] or "relative")
    assert imported <= {"__future__", "dataclasses", "math", "statistics"}


@pytest.mark.parametrize("kwargs", [{"target_p95_ms": 0}, {"kp": -1}, {"min_weight": 60, "home_weight": 50},
                                    {"min_weight": 100}, {"input_alpha": 0}, {"sample_interval_s": 0},
                                    {"ki": float("nan")}, {"max_dt_s": 1}])
def test_invalid_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        PIDConfig(**kwargs)
