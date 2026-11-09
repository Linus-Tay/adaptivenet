"""End-to-end loop on the REAL local gateway app (in-process, no network, no AWS):

inject degraded latency (POST /admin/faults) -> probe -> feature snapshot -> predict
-> review -> GatewayActuator.set_route (PUT /admin/routes) -> verify -> repeat until
user-facing latency recovers.

Region health is derived from the gateway's live fault table, so the "world" reacts
to the gateway's real route weight. Time is a fake clock so cooldowns run instantly.
"""
from __future__ import annotations
import time

import pytest
from fastapi.testclient import TestClient

from adaptivenet import gateway
from adaptivenet.ingestion.handoff import PROPOSAL_VERSION, conservative_rule_baseline
from adaptivenet.mitigation.actuators import GatewayActuator
from adaptivenet.mitigation.audit import AuditLog
from adaptivenet.mitigation.orchestrator import Mitigator, Policy

TOKEN = "e2e-token"
BASE_P95_MS = 40.0
SLO_P95_MS = 800.0


@pytest.fixture
def lab(monkeypatch, tmp_path):
    monkeypatch.setenv("LAB_TOKEN", TOKEN)
    gateway.faults.clear()
    monkeypatch.setattr(gateway, "weight_a", 100)
    client = TestClient(gateway.app)
    hdr = {"X-Lab-Token": TOKEN}
    now = [1_000.0]

    def region_health() -> dict:
        state = client.get("/admin/state", headers=hdr).json()
        out = {}
        for r in ("region-a", "region-b"):
            f = state["faults"].get(r, {})
            out[r] = {"status": "ok", "age_seconds": 0, "samples": 5,
                      "p95_ms": BASE_P95_MS + f.get("latency_ms", 0),
                      "error_ratio": f.get("error_rate", 0.0)}
        return out

    def user_p95() -> float:
        """What a client of the gateway actually sees (worst region carrying traffic)."""
        h, w = region_health(), client.get("/admin/state", headers=hdr).json()["region_a_percent"]
        carrying = [r for r, share in (("region-a", w), ("region-b", 100 - w)) if share > 0]
        return max(h[r]["p95_ms"] for r in carrying)

    def snapshot() -> dict:
        h = region_health()
        def region(r):
            return {"features": {"app_up": 1, "memory_available_pct": 60, "cpu_busy_pct": 30,
                                 "http_p95_ms": h[r]["p95_ms"], "http_error_ratio": h[r]["error_ratio"]},
                    "quality": {"internal": "fresh"}}
        return {"snapshot_id": f"features-{int(now[0])}", "computed_at_unix": time.time(),
                "ready_for_model": True, "regions": {r: region(r) for r in ("region-a", "region-b")}}

    def sleep(s):
        now[0] += s

    mitigator = Mitigator(
        GatewayActuator(base_url="http://testserver", token=TOKEN, client=client),
        region_health, AuditLog(tmp_path / "audit.jsonl", clock=lambda: now[0]),
        Policy(mode="enforce", cooldown_s=300, max_actions_per_hour=10, verify_interval_s=5),
        clock=lambda: now[0], sleep=sleep)
    return type("Lab", (), dict(client=client, hdr=hdr, now=now, m=mitigator, health=region_health,
                                user_p95=staticmethod(user_p95), snapshot=staticmethod(snapshot)))


def latency_predictor(snap: dict, current_a: int) -> dict:
    """Minimal stand-in for a trained model: step traffic 25% away from a slow region."""
    f = {r: snap["regions"][r]["features"] for r in ("region-a", "region-b")}
    a_bad, b_bad = (f[r]["http_p95_ms"] >= SLO_P95_MS for r in ("region-a", "region-b"))
    base = {"contract_version": PROPOSAL_VERSION, "snapshot_id": snap["snapshot_id"], "confidence": 0.9}
    if a_bad and not b_bad and current_a > 0:
        return {**base, "action": "shift_traffic", "target_region_a_percent": max(0, current_a - 25),
                "evidence": [f"region-a:p95={f['region-a']['http_p95_ms']}ms"]}
    if b_bad and not a_bad and current_a < 100:
        return {**base, "action": "shift_traffic", "target_region_a_percent": min(100, current_a + 25),
                "evidence": [f"region-b:p95={f['region-b']['http_p95_ms']}ms"]}
    return {**base, "action": "hold", "evidence": ["no_slo_breach"]}


def inject(lab, region="region-a", latency_ms=1500):
    r = lab.client.post("/admin/faults", headers=lab.hdr,
                        json={"region": region, "latency_ms": latency_ms, "duration_seconds": 120})
    assert r.status_code == 200


def test_degraded_latency_is_mitigated_until_users_recover(lab):
    assert lab.user_p95() < SLO_P95_MS
    inject(lab)
    assert lab.user_p95() >= SLO_P95_MS                       # 1. incident is visible

    cycles = []
    for _ in range(6):
        if lab.user_p95() < SLO_P95_MS:
            break
        snap = lab.snapshot()
        current = lab.client.get("/admin/state", headers=lab.hdr).json()["region_a_percent"]
        result = lab.m.run_cycle(snap, latency_predictor(snap, current))   # 2. predict -> 3. act
        cycles.append(result)
        assert result["status"] == "recovered", result
        lab.now[0] += 301                                     # wait out cooldown

    state = lab.client.get("/admin/state", headers=lab.hdr).json()
    assert state["region_a_percent"] == 0                     # 100 -> 75 -> 50 -> 25 -> 0
    assert len(cycles) == 4
    assert lab.user_p95() < SLO_P95_MS                        # 4. users actually recovered
    events = [e["event"] for e in lab.m.audit.read()]
    assert events.count("step_applied") == 4 and "rollback" not in events


def test_per_step_verification_only_checks_destination(lab):
    """Documents a gap: one 25% step is reported 'recovered' while users still see the slow region."""
    inject(lab)
    snap = lab.snapshot()
    result = lab.m.run_cycle(snap, latency_predictor(snap, 100))
    assert result["status"] == "recovered"
    assert lab.client.get("/admin/state", headers=lab.hdr).json()["region_a_percent"] == 75
    assert lab.user_p95() >= SLO_P95_MS


def test_degraded_destination_is_rolled_back_on_real_gateway(lab):
    inject(lab, "region-a", 1500)
    snap = lab.snapshot()
    # destination degrades right after the shift lands
    real_probe = lab.m.probe
    calls = {"n": 0}
    def probe():
        calls["n"] += 1
        h = real_probe()
        if calls["n"] > 1:   # first call is the pre-step baseline
            h["region-b"] = {**h["region-b"], "p95_ms": 2000}
        return h
    lab.m.probe = probe
    result = lab.m.run_cycle(snap, latency_predictor(snap, 100))
    assert result["status"] == "rolled_back"
    assert lab.client.get("/admin/state", headers=lab.hdr).json()["region_a_percent"] == 100


def test_shipped_baseline_predictor_never_mitigates_latency(lab):
    """Documents a gap: ml/predictor.py (rule baseline) cannot propose shift/scale."""
    inject(lab)
    snap = lab.snapshot()
    proposal = conservative_rule_baseline({"ready_for_inference": True, "snapshot_id": snap["snapshot_id"],
                                           "features": {r: {"app_up": 1, "cpu_busy_pct": 30, "memory_available_pct": 60}
                                                        for r in ("region-a", "region-b")}})
    assert proposal["action"] == "hold"
    assert lab.m.run_cycle(snap, proposal)["status"] == "held"
    assert lab.client.get("/admin/state", headers=lab.hdr).json()["region_a_percent"] == 100
