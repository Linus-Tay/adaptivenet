"""/controller/tick against a fake gateway: no network, no AWS."""
import json
import pytest
from fastapi.testclient import TestClient
from adaptivenet.control_api import app
from adaptivenet.controller import routes
from adaptivenet.controller.engine import Controller

GOOD = {"status": "ok", "age_seconds": 0, "samples": 5, "error_ratio": 0, "p95_ms": 20}
BAD = {"status": "ok", "age_seconds": 0, "samples": 5, "error_ratio": .8, "p95_ms": 1200}

@pytest.fixture
def lab(monkeypatch, tmp_path):
    world = {"route": 100, "puts": []}
    async def gateway_json(method, path, body=None):
        if method == "PUT":
            world["puts"].append(body["region_a_percent"]); world["route"] = body["region_a_percent"]
        return {"region_a_percent": world["route"]}
    async def collect():
        return {"region-a": BAD, "region-b": GOOD}
    monkeypatch.setenv("LAB_TOKEN", "t")
    monkeypatch.setenv("LOCAL_AUTO_MITIGATION", "true")
    monkeypatch.setenv("MITIGATION_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MITIGATION_KILL_SWITCH_FILE", str(tmp_path / "off"))
    monkeypatch.setattr(routes, "gateway_json", gateway_json)
    monkeypatch.setattr(routes, "collect", collect)
    monkeypatch.setattr(routes, "engine", Controller())
    client = TestClient(app)
    world["tick"] = lambda: client.post("/controller/tick", headers={"X-Lab-Token": "t"})
    world["dir"] = tmp_path
    return world

def test_tick_applies_one_bounded_audited_step(lab):
    for _ in range(2):
        assert lab["tick"]().json()["applied"] is False
    body = lab["tick"]().json()
    assert body["applied"] is True and body["applied_target_region_a_percent"] == 75 and lab["puts"] == [75]
    record = json.loads((lab["dir"] / "audit.jsonl").read_text())
    assert record["event"] == "step_applied" and record["previous"] == 100 and record["step"]["target"] == 75

def test_tick_respects_kill_switch(lab):
    (lab["dir"] / "off").write_text("")
    for _ in range(3):
        body = lab["tick"]().json()
    assert body["applied"] is False and body["blocked_reason"] == "kill_switch_engaged" and lab["puts"] == []

def test_tick_without_automation_only_proposes(lab, monkeypatch):
    monkeypatch.setenv("LOCAL_AUTO_MITIGATION", "false")
    for _ in range(3):
        body = lab["tick"]().json()
    assert body["decision"]["action"] == "propose_shift" and body["applied"] is False and lab["puts"] == []

@pytest.mark.parametrize("current,target,expected", [(100, 0, 75), (0, 100, 25), (10, 0, 0), (90, 100, 100)])
def test_bounded_target(current, target, expected):
    assert routes.bounded_target(current, target) == expected
