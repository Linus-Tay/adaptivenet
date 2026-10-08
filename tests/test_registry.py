from fastapi.testclient import TestClient
from adaptivenet.control_api import app

def body():
    return {"name": "Demo", "mode": "local", "endpoints": [
        {"endpoint_id": "a", "region": "region-a", "url": "http://localhost:8101"}]}

def test_persistence_and_no_implied_connection(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_TOKEN", "test")
    monkeypatch.setenv("REGISTRY_DB_PATH", str(tmp_path / "registry.db"))
    headers = {"X-Lab-Token": "test"}
    with TestClient(app) as client:
        assert client.post("/applications", json=body()).status_code == 401
        response = client.post("/applications", headers=headers, json=body())
        assert response.status_code == 201
        record = response.json()
        assert record["automation"] == "disabled"
        assert set(record["connections"].values()) == {"not_connected"}
    with TestClient(app) as another_client:
        assert another_client.get("/applications", headers=headers).json()["items"][0]["id"] == record["id"]

def test_rejects_unsafe_config_and_duplicates(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_TOKEN", "test")
    monkeypatch.setenv("REGISTRY_DB_PATH", str(tmp_path / "registry.db"))
    with TestClient(app) as client:
        for url in ["ftp://example.com", "https://user:secret@example.com", "https://example.com?token=secret"]:
            data = body(); data["endpoints"][0]["url"] = url
            assert client.post("/applications", headers={"X-Lab-Token":"test"}, json=data).status_code == 422
        data = body(); data["endpoints"] *= 2
        assert client.post("/applications", headers={"X-Lab-Token":"test"}, json=data).status_code == 422
