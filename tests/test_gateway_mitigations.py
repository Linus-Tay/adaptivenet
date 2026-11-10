"""Request-level mitigations in the local gateway: off by default, bounded, and always labelled."""
import asyncio
import pytest
from fastapi.testclient import TestClient
from adaptivenet import gateway

H = {"X-Lab-Token": "unit-test-token"}

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("LAB_TOKEN", "unit-test-token")
    def reset():
        gateway.faults.clear()
        gateway.weight_a, gateway.mitigations, gateway.last_good, gateway.in_flight = 100, gateway.Mitigations(), None, 0
    reset()
    async def upstream(region):
        return 200, {"region": region, "items": []}
    monkeypatch.setattr(gateway, "upstream", upstream)
    yield TestClient(gateway.app)
    reset()

def fault(client, **kw):
    assert client.post("/admin/faults", headers=H, json={"region": "region-a", **kw}).status_code == 200

def test_off_by_default_so_faults_stay_visible(client):
    fault(client, error_rate=1)
    r = client.get("/api/catalog")
    assert r.status_code == 503 and "x-mitigation" not in r.headers
    assert client.get("/admin/state", headers=H).json()["mitigations"] == gateway.Mitigations().model_dump()

def test_changing_mitigations_requires_token_and_bounds(client):
    assert client.put("/admin/mitigations", json={"failover": True}).status_code == 401
    assert client.delete("/admin/mitigations").status_code == 401
    for body in [{"attempt_timeout_ms": 10}, {"serve_stale_seconds": 3600}, {"max_in_flight": -1}]:
        assert client.put("/admin/mitigations", headers=H, json=body).status_code == 422

def test_failover_retries_once_on_the_other_region(client):
    fault(client, error_rate=1)
    client.put("/admin/mitigations", headers=H, json={"failover": True})
    r = client.get("/api/catalog")
    assert r.status_code == 200 and r.json()["region"] == "region-b" and r.headers["x-mitigation"] == "failover"

def test_failover_covers_a_slow_region(client):
    fault(client, latency_ms=1500)
    client.put("/admin/mitigations", headers=H, json={"failover": True, "attempt_timeout_ms": 50})
    r = client.get("/api/catalog")
    assert r.status_code == 200 and r.json()["region"] == "region-b" and r.headers["x-mitigation"] == "failover"

def test_failover_does_not_hide_a_total_outage(client, monkeypatch):
    calls = []
    async def down(region):
        calls.append(region); return 502, {"region": region, "error": "upstream unavailable"}
    monkeypatch.setattr(gateway, "upstream", down)
    client.put("/admin/mitigations", headers=H, json={"failover": True})
    r = client.get("/api/catalog")
    assert r.status_code == 502 and "x-mitigation" not in r.headers
    assert calls == ["region-a", "region-b"]  # exactly one retry, never a loop

def test_serve_stale_answers_from_last_good_and_expires(client, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(gateway.time, "monotonic", lambda: clock[0])
    client.put("/admin/mitigations", headers=H, json={"serve_stale_seconds": 60})
    assert "x-mitigation" not in client.get("/api/catalog").headers  # fills the cache
    fault(client, error_rate=1, duration_seconds=120)
    r = client.get("/api/catalog")
    assert r.status_code == 200 and r.headers["x-mitigation"] == "stale" and r.json()["region"] == "region-a"
    clock[0] += 61
    assert client.get("/api/catalog").status_code == 503  # too old to pass off as an answer

def test_serve_stale_has_nothing_to_serve_before_a_success(client):
    fault(client, error_rate=1)
    client.put("/admin/mitigations", headers=H, json={"serve_stale_seconds": 60})
    assert client.get("/api/catalog").status_code == 503

def test_load_shedding_refuses_above_the_cap_and_recovers(client):
    client.put("/admin/mitigations", headers=H, json={"max_in_flight": 2})
    async def run():
        release = asyncio.Event()
        async def slow(region):
            await release.wait(); return 200, {"region": region}
        gateway.upstream = slow
        held = [asyncio.create_task(gateway.catalog()) for _ in range(2)]
        await asyncio.sleep(0)
        shed = await gateway.catalog()
        release.set()
        done = await asyncio.gather(*held)
        return shed, done, await gateway.catalog()
    shed, done, after = asyncio.run(run())
    assert shed.status_code == 429 and shed.headers["x-mitigation"] == "shed" and shed.headers["retry-after"] == "1"
    assert [r.status_code for r in done] == [200, 200] and after.status_code == 200 and gateway.in_flight == 0

def test_delete_resets_everything(client):
    client.put("/admin/mitigations", headers=H, json={"failover": True, "serve_stale_seconds": 60, "max_in_flight": 5})
    client.get("/api/catalog")
    assert client.delete("/admin/mitigations", headers=H).json() == gateway.Mitigations().model_dump()
    assert gateway.last_good is None
