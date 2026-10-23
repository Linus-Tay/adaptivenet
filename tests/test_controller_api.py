"""POST /controller/tick end to end with fake probes and a fake gateway (no network)."""
from __future__ import annotations
import json
import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from adaptivenet import control_api
from adaptivenet.controller import routes

HOST = {'cpu_busy_pct': 5.0, 'node_load1': 0.2, 'memory_available_bytes': 6_000_000_000, 'memory_total_bytes': 8_000_000_000}

def view(latency=20.0, errors=0.0):
    return {'status': 'ok', 'age_seconds': 0, 'samples': 5, 'error_ratio': errors, 'latency_stat': 'median',
            'latency_ms': latency, 'max_ms': latency, 'observed_at': 'x', 'source': 'test', 'host': HOST}

class FakeLab:
    def __init__(self):
        self.weight = 100
        self.a_user_path_latency = 20.0
        self.routes = []
    async def collect(self):
        return {'region-a': view(), 'region-b': view()}
    async def collect_external(self):
        return {'region-a': view(self.a_user_path_latency), 'region-b': view()}
    async def collect_global(self):
        return view(self.a_user_path_latency if self.weight > 50 else 20.0)
    async def forward(self, method, path, body=None):
        if method == 'PUT':
            self.weight = body['region_a_percent']; self.routes.append(self.weight)
        return JSONResponse({'region_a_percent': self.weight, 'faults': {}})

@pytest.fixture
def lab(monkeypatch):
    fake = FakeLab()
    monkeypatch.setenv('LAB_TOKEN', 't')
    monkeypatch.setenv('LOCAL_AUTO_MITIGATION', 'true')
    monkeypatch.setattr(routes, 'collect', fake.collect)
    monkeypatch.setattr(routes, 'collect_external', fake.collect_external)
    monkeypatch.setattr(routes, 'collect_global', fake.collect_global)
    monkeypatch.setattr(routes, 'forward', fake.forward)
    monkeypatch.setattr(routes.actuator, 'forward', fake.forward)
    routes.automation_override = None
    routes.engine.reset()
    routes.engine.cooldown_seconds = 0
    return fake

H = {'X-Lab-Token': 't'}

def test_tick_returns_200_and_holds_when_healthy(lab):
    with TestClient(control_api.app) as client:
        r = client.post('/controller/tick', headers=H)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body['decision']['action'] == 'hold' and body['proposal']['action'] == 'hold'
        assert body['applied'] is False and body['mode'] == 'combined'
        assert client.post('/controller/tick').status_code == 401

def test_tick_acts_on_sustained_network_degradation_and_verifies(lab):
    lab.a_user_path_latency = 1500.0
    with TestClient(control_api.app) as client:
        actions = [client.post('/controller/tick', headers=H).json()['decision']['action'] for _ in range(3)]
        assert actions == ['hold', 'hold', 'shift_traffic']
        assert lab.routes == [75]
        journal = client.get('/controller/journal', headers=H).json()
        assert journal['actions'][0]['actuation']['status'] == 'applied'
        assert journal['pending_verification'] == [1]

def test_automation_off_records_decision_without_acting(lab):
    lab.a_user_path_latency = 1500.0
    with TestClient(control_api.app) as client:
        client.put('/controller/automation', headers=H, json={'enabled': False})
        for _ in range(3):
            body = client.post('/controller/tick', headers=H).json()
        assert body['decision']['action'] == 'shift_traffic' and body['applied'] is False
        assert lab.routes == []
