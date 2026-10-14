from datetime import datetime, timezone
import pytest
import asyncio
import httpx
from fastapi.testclient import TestClient
from adaptivenet.collectors.atlas.client import configured_measurements, normalize_ping, fetch_latest
from adaptivenet.control_api import app


def test_config(monkeypatch):
    monkeypatch.setenv("ATLAS_MEASUREMENTS", "region-a=123,region-b=456")
    assert configured_measurements() == {"region-a": 123, "region-b": 456}
    monkeypatch.setenv("ATLAS_MEASUREMENTS", "region-a=wrong")
    with pytest.raises(ValueError): configured_measurements()


def test_ping_and_staleness():
    now = datetime(2026, 10, 12, tzinfo=timezone.utc)
    raw = {"timestamp": int(now.timestamp())-30, "prb_id": 9, "sent": 3,
           "result": [{"rtt": 10}, {"x": "timeout"}, {"rtt": 20}]}
    sample = normalize_ping(42, "region-a", raw, now)
    assert sample["rtt_ms"] == 15
    assert sample["packet_loss_percent"] == 33.33
    assert sample["status"] == "ok"
    assert normalize_ping(42, "region-a", {**raw, "timestamp": 10}, now)["status"] == "stale"


def test_fetch_latest_mocked():
    async def handler(request):
        assert request.url.path == "/api/v2/measurements/42/latest/"
        return httpx.Response(200, json={"9": [{"timestamp": 1780000000, "prb_id": 9,
                                                     "sent": 2, "result": [{"rtt": 12}, {"rtt": 14}]}]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_latest(42, "region-a", client)
    result = asyncio.run(run())
    assert result["sample_count"] == 1
    assert result["probes"][0]["rtt_ms"] == 13


def test_access_control_and_unconfigured(monkeypatch):
    monkeypatch.setenv("LAB_TOKEN", "secret")
    monkeypatch.delenv("ATLAS_MEASUREMENTS", raising=False)
    with TestClient(app) as client:
        assert client.get("/telemetry/atlas/latest").status_code == 401
        assert client.get("/telemetry/atlas/latest", headers={"X-Lab-Token": "secret"}).json()["status"] == "not_configured"
