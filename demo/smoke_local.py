"""Run the whole demo natively (no Docker) and check the gray-failure story end to end.

  PYTHONPATH=services:. python -m demo.smoke_local

Starts origins, gateway, collector, feature worker, ML and controller as
local processes on loopback ports, waits for healthy telemetry, injects a
gateway-path latency fault on region-a, and checks that:
* AWS-only stays healthy, AWS+RIPE+HTTP reports network degradation in region-a;
* the decision path proposes failover and the safety controller shifts the
  local gateway in bounded steps.
Fast cadences are used so it finishes in a few minutes. Writes only to a temp folder.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time

import httpx


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    data = Path(tempfile.mkdtemp(prefix="adaptivenet-demo-smoke-"))
    ports = {k: free_port() for k in ("a", "b", "gw", "ml")}
    url = {k: f"http://127.0.0.1:{p}" for k, p in ports.items()}
    token = secrets.token_hex(8)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(root/'services'), str(root)]), "LAB_TOKEN": token,
           "DEMO_DATA_DIR": str(data), "REGION_A_URL": url["a"], "REGION_B_URL": url["b"],
           "GATEWAY_URL": url["gw"], "OBSERVER_GATEWAY_URL": url["gw"], "ML_URL": url["ml"],
           "WITNESS_INTERVAL_SEC": "2", "INTERNAL_INTERVAL_SEC": "4", "EXTERNAL_INTERVAL_SEC": "6",
           "DECISION_INTERVAL_SEC": "3", "LAB_PROBES": "3", "USER_RPS": "3"}
    env.pop("ADAPTIVENET_SHADOW_MODEL", None)
    py = sys.executable
    uv = lambda mod, port, **extra: ([py, "-m", "uvicorn", mod, "--host", "127.0.0.1", "--port", str(port),
                                      "--log-level", "warning"], extra)
    specs = [uv("demo.lab.origin:app", ports["a"], REGION="region-a"),
             uv("demo.lab.origin:app", ports["b"], REGION="region-b"),
             uv("adaptivenet.gateway:app", ports["gw"]),
             uv("demo.lab.ml_service:app", ports["ml"]),
             ([py, "-m", "demo.lab.traffic"], {}), ([py, "-m", "demo.lab.collector"], {}),
             ([py, "-m", "demo.lab.worker"], {}),
             ([py, "-m", "demo.lab.controller"], {"GATEWAY_URL": url["gw"]})]
    logs = data / "logs"
    logs.mkdir()
    procs = []
    try:
        for i, (cmd, extra) in enumerate(specs):
            out = (logs / f"{i}.log").open("w", encoding="utf-8")
            procs.append(subprocess.Popen(cmd, cwd=root, env={**env, **extra}, stdout=out, stderr=out))
            if i == 3:
                time.sleep(2)  # servers up before clients
        client = httpx.Client(timeout=10, trust_env=False)

        def assessment():
            r = client.get(url["ml"] + "/v1/assessment")
            return r.json() if r.status_code == 200 else None

        def wait(pred, timeout, what):
            end = time.time() + timeout
            while time.time() < end:
                a = assessment()
                if a and pred(a):
                    return a
                time.sleep(2)
            raise SystemExit(f"FAIL: timed out waiting for {what}; logs in {logs}")

        healthy = wait(lambda a: all(d["condition"] == "healthy" for d in a["detectors"].values()), 90,
                       "all feature groups healthy")
        print("baseline healthy:", {g: d["condition"] for g, d in healthy["detectors"].items()})
        r = client.post(url["gw"] + "/admin/faults", headers={"X-Lab-Token": token},
                        json={"region": "region-a", "latency_ms": 700, "duration_seconds": 120})
        r.raise_for_status()
        print("injected 700 ms latency on the gateway path to region-a (the origin itself is untouched)")
        degraded = wait(lambda a: a["detectors"]["aws_ripe_http"]["condition"] == "network_degradation", 90,
                        "external groups to detect network degradation")
        groups = {g: d["condition"] for g, d in degraded["detectors"].items()}
        print("during fault:", groups)
        assert groups["aws_only"] == "healthy", "AWS-only should not see a path-only fault"
        end = time.time() + 120
        weight = 100
        while time.time() < end and weight == 100:
            state = client.get(url["gw"] + "/admin/state", headers={"X-Lab-Token": token}).json()
            weight = state["region_a_percent"]
            time.sleep(2)
        decisions = [json.loads(l) for l in (data / "decisions.jsonl").read_text(encoding="utf-8").splitlines()]
        failovers = [d for d in decisions if d.get("decision") == "failover"]
        print(f"decisions: {len(decisions)}, failover decisions: {len(failovers)}, gateway region-a weight: {weight}%")
        if weight == 100:
            last = failovers[-1]["executed_locally"] if failovers else decisions[-1]
            raise SystemExit(f"FAIL: controller did not shift; last: {json.dumps(last)[:600]}")
        assert 0 <= weight <= 75
        verify = None
        end = time.time() + 60
        while time.time() < end and verify is None:
            time.sleep(3)
            for line in (data / "decisions.jsonl").read_text(encoding="utf-8").splitlines():
                record = json.loads(line).get("executed_locally") or {}
                if "verify" in record:
                    verify = record["verify"]
        print("client verification after the first step:", verify)
        final = client.get(url["gw"] + "/admin/state", headers={"X-Lab-Token": token}).json()
        print("gateway region-a weight now:", final["region_a_percent"], "%")
        print("PASS: gray failure seen only with external signals; bounded failover applied on the local gateway")
        return 0
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        print("smoke data and logs:", data)


if __name__ == "__main__":
    raise SystemExit(main())
