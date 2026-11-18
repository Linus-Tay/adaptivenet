"""Real HTTP smoke: auth, fault, isolation, route change and automatic expiry."""
import os
import secrets
import time
import httpx
from local import start, stop, ROOT

env = {**os.environ, "PYTHONPATH": str(ROOT / "services"), "LAB_TOKEN": secrets.token_hex(24)}
processes = start(env)
headers = {"X-Lab-Token": env["LAB_TOKEN"]}
try:
    with httpx.Client(timeout=5, trust_env=False) as client:
        for port in [8101,8102,8080,8000]:
            for attempt in range(100):
                if any(p.poll() is not None for p in processes):
                    raise RuntimeError("A service could not start; check for port conflicts")
                try:
                    if client.get(f"http://127.0.0.1:{port}/healthz").is_success:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.1)
            else:
                raise RuntimeError(f"Port {port} did not become ready")
        assert client.get("http://127.0.0.1:8000/state").status_code == 401
        assert client.get("http://127.0.0.1:8080/api/catalog").json()["region"] == "region-a"
        assert client.post("http://127.0.0.1:8000/experiments",headers=headers,json={
            "region":"region-a","error_rate":1,"duration_seconds":2}).is_success
        assert client.get("http://127.0.0.1:8080/api/catalog").status_code == 503
        assert client.get("http://127.0.0.1:8101/healthz").is_success
        assert client.get("http://127.0.0.1:8000/state",headers=headers).is_success
        assert client.put("http://127.0.0.1:8000/routes",headers=headers,json={"region_a_percent":0}).is_success
        assert client.get("http://127.0.0.1:8080/api/catalog").json()["region"] == "region-b"
        time.sleep(2.1)
        client.put("http://127.0.0.1:8000/routes",headers=headers,json={"region_a_percent":100}).raise_for_status()
        assert client.get("http://127.0.0.1:8080/api/catalog").is_success
        assert client.post("http://127.0.0.1:8000/experiments",headers=headers,json={
            "region":"control-plane","duration_seconds":999}).status_code == 422
        client.post("http://127.0.0.1:8000/experiments",headers=headers,json={
            "region":"region-a","latency_ms":150,"duration_seconds":10}).raise_for_status()
        begin=time.monotonic()
        assert client.get("http://127.0.0.1:8080/api/catalog").is_success
        assert time.monotonic()-begin >= .15
        client.post("http://127.0.0.1:8000/experiments",headers=headers,json={
            "region":"region-a","loss_pct":100,"duration_seconds":10}).raise_for_status()
        assert client.get("http://127.0.0.1:8080/api/catalog").status_code == 504
        client.delete("http://127.0.0.1:8000/experiments",headers=headers).raise_for_status()
        assert not client.get("http://127.0.0.1:8000/state",headers=headers).json()["faults"]
    print("PASS: auth, baseline, fault, healthy control plane, B routing, expiry, bounds, latency, loss, cleanup")
finally:
    stop(processes)
