"""Closed loop with fakes: gates, actuation, read-back, recovery verification, rollback."""
import time
import pytest
from adaptivenet.mitigation.audit import AuditLog
from adaptivenet.mitigation.orchestrator import Mitigator, Policy

GOOD = {"status": "ok", "age_seconds": 0, "samples": 5, "error_ratio": 0, "p95_ms": 50}
BAD = {"status": "ok", "age_seconds": 0, "samples": 5, "error_ratio": .9, "p95_ms": 2000}

class FakeActuator:
    def __init__(self, route=50, replicas=None, can_scale=True, fail_write=False):
        self.route, self.replicas, self.can_scale, self.fail_write = route, replicas or {"region-a": 2, "region-b": 2}, can_scale, fail_write
        self.writes = []
    def read_route(self): return self.route
    def set_route(self, v):
        if self.fail_write: raise RuntimeError("x")
        self.writes.append(("route", v)); self.route = v
    def read_replicas(self, r): return self.replicas[r]
    def read_capacity(self, r): return self.replicas[r]
    def read_ready_replicas(self, r): return self.replicas[r]
    def set_replicas(self, r, n): self.writes.append(("scale", r, n)); self.replicas[r] = n

def snap(cpu_b=20, age=5):
    f = lambda cpu: {"features": {"app_up": 1, "memory_available_pct": 60, "cpu_busy_pct": cpu}, "quality": {"internal": "fresh"}}
    return {"snapshot_id": "features-1", "computed_at_unix": time.time() - age, "ready_for_model": True,
            "regions": {"region-a": f(90), "region-b": f(cpu_b)}}

SHIFT = {"contract_version": "adaptivenet.proposal.v1", "snapshot_id": "features-1", "action": "shift_traffic",
         "confidence": .9, "target_region_a_percent": 25, "evidence": ["a:p95_rising"]}

def make(tmp_path, act, health, **pol):
    seq = iter(health)
    last = {}
    def probe():
        nonlocal last
        last = next(seq, last); return last
    now = [1000.0]
    def sleep(s): now[0] += s
    m = Mitigator(act, probe, AuditLog(tmp_path / "a.jsonl", clock=lambda: now[0]),
                  Policy(**{"mode": "enforce", "verify_interval_s": 1, **pol}), clock=lambda: now[0], sleep=sleep)
    return m

def healthy(n=10): return [{"region-a": BAD, "region-b": GOOD}] * n

def test_shadow_never_writes(tmp_path):
    a = FakeActuator(); m = make(tmp_path, a, healthy(), mode="shadow")
    r = m.run_cycle(snap(), SHIFT)
    assert r["status"] == "shadow" and a.writes == []

def test_shift_applied_and_recovery_verified(tmp_path):
    a = FakeActuator(); m = make(tmp_path, a, healthy())
    r = m.run_cycle(snap(), SHIFT)
    assert r["status"] == "recovered" and a.route == 25
    events = [e["event"] for e in m.audit.read()]
    assert events.index("step_applied") < events.index("verification")

def test_destination_degrades_rolls_back(tmp_path):
    a = FakeActuator(); m = make(tmp_path, a, [{"region-b": GOOD}] + [{"region-b": BAD}] * 5)
    r = m.run_cycle(snap(), SHIFT)
    assert r["status"] == "rolled_back" and a.route == 50

def test_verification_timeout_rolls_back_traffic(tmp_path):
    a = FakeActuator(); m = make(tmp_path, a, [{"region-b": {"status": "stale"}}], verify_timeout_s=10)
    assert m.run_cycle(snap(), SHIFT)["status"] == "rolled_back" and a.route == 50

def test_apply_failure_reports_rollback(tmp_path):
    a = FakeActuator(fail_write=True); m = make(tmp_path, a, healthy())
    assert m.run_cycle(snap(), SHIFT)["status"] == "rollback_failed"

def test_hot_destination_scales_before_shifting(tmp_path):
    a = FakeActuator(); m = make(tmp_path, a, healthy())
    r = m.run_cycle(snap(cpu_b=80), SHIFT)
    assert [w[0] for w in a.writes] == ["scale", "route"] and a.replicas["region-b"] == 3 and r["status"] == "recovered"

def test_scale_out_proposal(tmp_path):
    a = FakeActuator(); m = make(tmp_path, a, [{"region-a": GOOD}] * 5)
    p = {**SHIFT, "action": "scale_out", "region": "region-a", "target_replicas": 4}
    p.pop("target_region_a_percent")
    assert m.run_cycle(snap(), p)["status"] == "recovered" and a.replicas["region-a"] == 4

def test_scale_step_and_unreviewed_rejected(tmp_path):
    a = FakeActuator(); m = make(tmp_path, a, healthy())
    p = {**SHIFT, "action": "scale_out", "region": "region-a", "target_replicas": 6}
    assert m.run_cycle(snap(), p)["status"] == "rejected" and a.writes == []
    assert m.run_cycle(snap(), {**SHIFT, "confidence": .5})["status"] == "rejected"

def test_cooldown_duplicate_rate_limit_and_kill_switch(tmp_path):
    a = FakeActuator(); m = make(tmp_path, a, healthy(), cooldown_s=300)
    assert m.run_cycle(snap(), SHIFT)["status"] == "recovered"
    assert m.run_cycle(snap(), {**SHIFT, "target_region_a_percent": 0})["reason"] == "cooldown"
    m.policy.cooldown_s = 0
    a.route = 50
    assert m.run_cycle(snap(), SHIFT)["reason"] == "duplicate_proposal"
    ks = tmp_path / "off"; ks.write_text(""); m.policy.kill_switch_file = str(ks)
    assert m.run_cycle(snap(), {**SHIFT, "snapshot_id": "features-1", "target_region_a_percent": 30})["reason"] == "kill_switch_engaged"

def test_unreadable_actuator_blocks(tmp_path):
    a = FakeActuator()
    a.read_route = lambda: (_ for _ in ()).throw(OSError())
    assert make(tmp_path, a, healthy()).run_cycle(snap(), SHIFT)["status"] == "blocked"

def test_hold_does_nothing(tmp_path):
    a = FakeActuator()
    assert make(tmp_path, a, healthy()).run_cycle(snap(), {**SHIFT, "action": "hold"})["status"] == "held"


# ---- regressions -------------------------------------------------------------
from types import SimpleNamespace as NS
from adaptivenet.mitigation.actuators import GatewayActuator, KubernetesActuator

def test_probe_failure_during_verification_rolls_back(tmp_path):
    a = FakeActuator(); calls = [0]
    def probe():
        calls[0] += 1
        if calls[0] > 1: raise RuntimeError("probe died")
        return {"region-b": GOOD}
    m = make(tmp_path, a, [], verify_timeout_s=10); m.probe = probe
    r = m.run_cycle(snap(), SHIFT)
    assert r["status"] == "rolled_back" and a.route == 50
    assert [e["event"] for e in m.audit.read()][-2:] == ["rollback", "cycle_end"]

def test_probe_failure_before_write_aborts_without_writing(tmp_path):
    a = FakeActuator(); m = make(tmp_path, a, [])
    m.probe = lambda: (_ for _ in ()).throw(RuntimeError())
    assert m.run_cycle(snap(), SHIFT)["status"] == "aborted" and a.writes == []

@pytest.mark.parametrize("event", ["verification", "rollback", "cycle_end"])
def test_audit_failure_after_write_never_escapes_or_strands_traffic(tmp_path, event):
    a = FakeActuator(); m = make(tmp_path, a, healthy()); real = m.audit.append
    def append(ev, **kw):
        if ev in ("verification", event): raise OSError("disk full")
        return real(ev, **kw)
    m.audit.append = append
    r = m.run_cycle(snap(), SHIFT)
    assert r["status"] == "rolled_back" and a.route == 50 and r["steps"][0]["audit_error"] == "OSError"
    assert event != "cycle_end" or r["audit_error"] == "OSError"

def test_no_audit_means_no_action(tmp_path):
    a = FakeActuator(); m = make(tmp_path, a, healthy())
    m.audit.append = lambda *x, **k: (_ for _ in ()).throw(OSError())
    assert m.run_cycle(snap(), SHIFT)["reason"] == "audit_unavailable" and a.writes == []

def test_kill_switch_stops_later_plan_steps(tmp_path):
    ks = tmp_path / "off"; a = FakeActuator(); calls = [0]
    def probe():
        calls[0] += 1
        if calls[0] == 3: ks.write_text("")
        return {"region-b": GOOD}
    m = make(tmp_path, a, [], kill_switch_file=str(ks)); m.probe = probe
    r = m.run_cycle(snap(cpu_b=80), SHIFT)
    assert r["status"] == "halted" and a.writes == [("scale", "region-b", 3)]

def test_record_after_torn_audit_line_survives(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl", clock=lambda: 1.0)
    log.append("step_applied", idempotency_key="k1")
    with open(log.path, "ab") as fh: fh.write(b'{"ts": 1.0, "event": "step_\xff')
    log.append("step_applied", idempotency_key="k2")
    assert [r["idempotency_key"] for r in log.actions_since(0)] == ["k1", "k2"]

def test_audit_ignores_valid_json_that_is_not_a_record(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl", clock=lambda: 1.0)
    log.path.write_text('[1]\n{"event": "step_applied"}\n')
    assert log.read() == [] and log.actions_since(0) == []

def test_unreadable_audit_blocks_action(tmp_path):
    a = FakeActuator(); m = make(tmp_path, a, healthy())
    m.audit.actions_since = lambda s: (_ for _ in ()).throw(OSError())
    assert m.run_cycle(snap(), SHIFT)["reason"] == "audit_unreadable" and a.writes == []

@pytest.mark.parametrize("input_mode,expected", [("internal_only", "recovered"), ("combined", "rejected")])
def test_input_mode_selects_readiness_flag(tmp_path, input_mode, expected):
    s = {**snap(), "ready_for_model": False, "ready_for_internal_only_model": True}
    assert make(tmp_path, FakeActuator(), healthy(), input_mode=input_mode).run_cycle(s, SHIFT)["status"] == expected

@pytest.mark.parametrize("bad", [
    [], "shift", None,
    {**SHIFT, "confidence": float("nan")},
    {**SHIFT, "action": "scale_out", "region": ["region-a"], "target_replicas": 3},
])
def test_malformed_model_output_is_rejected_and_recorded(tmp_path, bad):
    a = FakeActuator(); m = make(tmp_path, a, healthy())
    assert m.run_cycle(snap(), bad)["status"] == "rejected" and a.writes == []
    assert [e["event"] for e in m.audit.read()] == ["proposal_received", "cycle_end"]

def test_unreadable_replicas_do_not_block_rerouting_but_block_scaling(tmp_path):
    a = FakeActuator(); a.read_capacity = lambda r: (_ for _ in ()).throw(ValueError())
    r = make(tmp_path / "1", a, healthy()).run_cycle(snap(cpu_b=80), SHIFT)
    assert r["status"] == "recovered" and a.route == 25 and a.writes == [("route", 25)]
    p = {**SHIFT, "action": "scale_out", "region": "region-a", "target_replicas": 4}
    r = make(tmp_path / "2", a, healthy()).run_cycle(snap(), p)
    assert r["status"] == "rejected" and "current_replicas_unknown" in r["reasons"]

def test_scale_out_waits_for_ready_pods(tmp_path):
    a = FakeActuator(); a.read_ready_replicas = lambda r: 2
    m = make(tmp_path, a, [{"region-a": GOOD}] * 5, verify_timeout_s=10)
    p = {**SHIFT, "action": "scale_out", "region": "region-a", "target_replicas": 4}
    r = m.run_cycle(snap(), p)
    assert r["status"] == "unverified" and set(r["steps"][0]["verification"]["samples"]) == {"pods_not_ready"}

def test_missing_lab_token_blocks_cleanly(tmp_path, monkeypatch):
    monkeypatch.delenv("LAB_TOKEN", raising=False)
    act = GatewayActuator(base_url="http://x", client=NS(request=lambda *a, **k: pytest.fail("no request expected")))
    r = make(tmp_path, act, healthy(), mode="shadow").run_cycle(snap(), SHIFT)
    assert r["reason"] == "actuator_state_unreadable" and "LAB_TOKEN" in r["detail"]

class FakeApps:
    def __init__(self, replicas=2, ready=2): self.replicas, self.ready = replicas, ready
    def read_namespaced_deployment_scale(self, *_): return NS(spec=NS(replicas=self.replicas))
    def read_namespaced_deployment_status(self, *_): return NS(status=NS(ready_replicas=self.ready))
    def patch_namespaced_deployment_scale(self, _n, _ns, body): self.replicas = body["spec"]["replicas"]

class FakeHpa:
    def __init__(self, minimum, maximum=10): self.spec = NS(min_replicas=minimum, max_replicas=maximum)
    def read_namespaced_horizontal_pod_autoscaler(self, *_): return NS(spec=self.spec)
    def patch_namespaced_horizontal_pod_autoscaler(self, _n, _ns, body):
        self.spec.min_replicas = body["spec"]["minReplicas"]
        self.spec.max_replicas = body["spec"].get("maxReplicas", self.spec.max_replicas)

HPA_TARGET = {"region-a": {"namespace": "x", "deployment": "a", "hpa": "a"}}

def test_hpa_without_min_replicas_defaults_to_one():
    assert KubernetesActuator(HPA_TARGET, FakeApps(), FakeHpa(None)).read_replicas("region-a") == 1

def test_capacity_is_running_replicas_not_hpa_minimum():
    k = KubernetesActuator(HPA_TARGET, FakeApps(replicas=5), FakeHpa(2))
    assert k.read_replicas("region-a") == 2 and k.read_capacity("region-a") == 5

def test_hpa_rollback_restores_max_replicas():
    h = FakeHpa(2, maximum=2); k = KubernetesActuator(HPA_TARGET, FakeApps(), h)
    k.set_replicas("region-a", 4); assert h.spec.max_replicas == 4
    k.set_replicas("region-a", 2); assert (h.spec.min_replicas, h.spec.max_replicas) == (2, 2)

def test_scale_out_below_running_count_is_rejected(tmp_path):
    a = GatewayActuator(base_url="http://x", token="t",
                        scaler=KubernetesActuator(HPA_TARGET, FakeApps(replicas=5), FakeHpa(2)),
                        client=NS(request=lambda *x, **y: NS(raise_for_status=lambda: None, json=lambda: {"region_a_percent": 50})))
    p = {**SHIFT, "action": "scale_out", "region": "region-a", "target_replicas": 3}
    r = make(tmp_path, a, healthy()).run_cycle(snap(), p)
    assert r["status"] == "rejected" and "scale_out_must_increase_replicas" in r["reasons"]

def test_null_p95_is_unknown_not_degraded(tmp_path):
    # A latency gap on the destination must wait for data, not roll back as "destination_degraded".
    gap = {**GOOD, "p95_ms": None}
    a = FakeActuator(); m = make(tmp_path, a, [{"region-b": gap}] * 3 + [{"region-b": GOOD}] * 5)  # first sample is the pre-change baseline
    r = m.run_cycle(snap(), SHIFT)
    assert r["status"] == "recovered" and a.route == 25
    assert r["steps"][0]["verification"]["samples"][:2] == ["unknown", "unknown"]

def test_all_requests_failing_is_degraded_even_without_p95(tmp_path):
    dead = {**GOOD, "error_ratio": 1, "p95_ms": None}
    a = FakeActuator(); m = make(tmp_path, a, [{"region-b": dead}] * 3)
    assert m.run_cycle(snap(), SHIFT)["status"] == "rolled_back" and a.route == 50

def test_settle_time_delays_verification(tmp_path):
    # Slow actuator (weighted DNS): don't judge the change before it has reached clients.
    a = FakeActuator(); m = make(tmp_path, a, healthy(), actuator_settle_s=600)
    start = m.clock()
    assert m.run_cycle(snap(), SHIFT)["status"] == "recovered"
    assert m.clock() - start >= 600

def test_settle_time_extends_cooldown_across_restart(tmp_path):
    # A change applied by an earlier process is still propagating: cooldown_s alone would allow a second move.
    a = FakeActuator(); m = make(tmp_path, a, healthy(), actuator_settle_s=600, cooldown_s=60)
    m.audit.append("step_applied", cycle_id="earlier", idempotency_key="k", step={"kind": "shift_traffic"})
    m.sleep(120)
    r = m.run_cycle(snap(), SHIFT)
    assert r["status"] == "blocked" and r["reason"] == "cooldown" and a.writes == []
