import pytest
from fastapi.testclient import TestClient
from adaptivenet import gateway
from adaptivenet.controller.local_gateway import GatewayRoute

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("LAB_TOKEN", "unit-test-token")
    gateway.faults.clear()
    gateway.weight_a = 100
    with TestClient(gateway.app, base_url="http://127.0.0.1") as client:
        yield client

def test_mutation_requires_token(client):
    assert client.put("/admin/routes",json={"region_a_percent":0}).status_code == 401


def test_gateway_adapter_does_not_overwrite_unexpected_route(client):
    route = GatewayRoute(client, "unit-test-token")
    assert route.read_weight() == 100
    route.request_weight(original=100, target=75)
    with pytest.raises(OSError):
        route.request_weight(original=100, target=50)
    assert route.read_weight() == 75


def test_regional_probe_is_authenticated_and_uses_gateway_faults(client):
    headers = {"X-Lab-Token": "unit-test-token"}
    assert client.get("/admin/probe/region-b").status_code == 401
    assert client.post(
        "/admin/faults", headers=headers, json={"region": "region-b", "error_rate": 1},
    ).status_code == 200
    response = client.get("/admin/probe/region-b", headers=headers)
    assert response.status_code == 503
    assert response.json()["region"] == "region-b"
    assert GatewayRoute(client, "unit-test-token").read_weight() == 100

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
