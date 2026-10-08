import pytest
from fastapi.testclient import TestClient
from adaptivenet import gateway

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("LAB_TOKEN", "unit-test-token")
    gateway.faults.clear()
    gateway.weight_a = 100
    return TestClient(gateway.app)

def test_mutation_requires_token(client):
    assert client.put("/admin/routes",json={"region_a_percent":0}).status_code == 401

def test_faults_are_bounded_and_target_restricted(client):
    headers={"X-Lab-Token":"unit-test-token"}
    for body in [{"region":"control-plane"},{"region":"region-a","duration_seconds":121},
                 {"region":"region-a","error_rate":2}]:
        assert client.post("/admin/faults",headers=headers,json=body).status_code == 422

def test_expired_fault_does_not_appear(client,monkeypatch):
    headers={"X-Lab-Token":"unit-test-token"}
    clock=[100.0]
    monkeypatch.setattr(gateway.time,"monotonic",lambda:clock[0])
    client.post("/admin/faults",headers=headers,json={"region":"region-a","duration_seconds":1})
    clock[0]=102
    assert client.get("/admin/state",headers=headers).json()["faults"] == {}
