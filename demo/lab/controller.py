"""Demo decision loop: ML assessment -> proposal -> safety review -> EKS decision.

Every tick:
1. read the shadow assessment from the ML service;
2. turn the selected feature group's detector result into an
   ``adaptivenet.proposal.v1`` (untrusted model output);
3. ask ``adaptivenet.controller.end_to_end.decide`` for a dry-run EKS decision
   (scale / failover / no-op) behind the independent safety reviewer;
4. execute ONLY a ``failover`` and ONLY on the local lab gateway, through the
   unchanged ``SafetyController(mode="local")`` (3 fresh observations, journal,
   bounded 25-point steps, client recovery verification, cooldown).
   ``scale`` is never executed here: the plan is shown, and live HPA writes stay
   behind ``scripts/end_to_end.py execute --approve-live-write``.

``DEMO_CONTROLLER_MODE``: ``local`` (default, actuates the lab gateway),
``review`` (decisions only; used for replay), or ``disabled``.
Must share the gateway's network namespace: the route adapter is loopback-only.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import logging
import os
from pathlib import Path
import time
import uuid

import httpx

from adaptivenet.controller.end_to_end import ALLOWLIST, decide
from adaptivenet.controller.journal import ActionJournal
from adaptivenet.controller.local_gateway import GatewayRoute
from adaptivenet.controller.recovery import HttpMeasurement, RecoveryWindow, DEADLINE_SEC
from adaptivenet.controller.safety import SafetyController
from adaptivenet.controller.scaling import HpaObservation

log = logging.getLogger("demo.controller")
REGIONS = ("region-a", "region-b")
FAILOVER_STEP = 25
SHIFT_CONDITIONS = ("network_degradation", "application_failure", "region_down")
MAX_LOG_BYTES = 2 * 1024 * 1024


def build_proposal(assessment: dict, group: str, current_region_a_percent: int) -> dict:
    """Map one detector result to proposal.v1. Still untrusted: the reviewer decides."""
    detector = assessment["detectors"][group]
    condition, region = detector.get("condition"), detector.get("affected_region")
    base = {"contract_version": "adaptivenet.proposal.v1", "snapshot_id": assessment["snapshot_id"],
            "model_id": f"{detector.get('detector', 'unknown')}:{group}",
            "evidence": [f"shadow_condition:{condition}", f"feature_group:{group}"]}
    if condition == "healthy":
        return {**base, "action": "hold", "confidence": 0.9}
    if region not in REGIONS:
        return {**base, "action": "investigate", "confidence": 0.5,
                "evidence": base["evidence"] + ["no_single_affected_region"]}
    reasons = detector["regions"][region]["reasons"]
    evidence = base["evidence"] + [f"affected_region:{region}"] + [f"{region}:{r}" for r in reasons]
    if condition == "compute_saturation":
        return {**base, "action": "scale_out", "confidence": 0.85, "affected_region": region, "evidence": evidence}
    if condition in SHIFT_CONDITIONS:
        delta = -FAILOVER_STEP if region == "region-a" else FAILOVER_STEP
        target = max(0, min(100, current_region_a_percent + delta))
        if target != current_region_a_percent:
            return {**base, "action": "shift_traffic", "confidence": 0.85, "affected_region": region,
                    "target_region_a_percent": target, "evidence": evidence}
    return {**base, "action": "investigate", "confidence": 0.6, "evidence": evidence}


def emulated_hpa(region: str, snapshot: dict, now: float, replicas: int = 2) -> HpaObservation:
    """Lab stand-in for the HPA read. CPU comes from the measured origin metric."""
    cpu = ((snapshot.get("regions") or {}).get(region) or {}).get("features", {}).get("cpu_busy_pct")
    return HpaObservation(
        target=ALLOWLIST[region], observed_at=now, metrics_observed_at=now,
        min_replicas=2, max_replicas=6, current=replicas, desired=replicas, ready=replicas,
        available=replicas, pending=0, able_to_scale=True, scaling_active=True,
        scaling_limited=False, cpu_target_pct=60,
        cpu_current_pct=int(cpu) if isinstance(cpu, (int, float)) else None)


class DecisionLog:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: dict) -> None:
        if self.path.exists() and self.path.stat().st_size > MAX_LOG_BYTES:
            os.replace(self.path, self.path.with_suffix(".jsonl.1"))
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str, allow_nan=False) + "\n")


class DemoLoop:
    def __init__(self, *, client: httpx.Client, ml_url: str, edge_url: str, gateway_url: str,
                 token: str, data_dir: Path, mode: str, group: str):
        self.client, self.ml, self.edge = client, ml_url.rstrip("/"), edge_url.rstrip("/")
        self.mode, self.group = mode, group
        self.log = DecisionLog(data_dir / "decisions.jsonl")
        self.route = None
        self.safety = None
        if mode == "local":
            self.route = GatewayRoute(httpx.Client(base_url=gateway_url, timeout=3, trust_env=False), token)
            journal = ActionJournal(data_dir / "controller-journal.db")
            self.safety = SafetyController(journal, self.route, mode="local")
        # Review mode has no gateway to read; assume an even split unless told otherwise.
        self.weight_fallback = int(os.environ.get("DEMO_REVIEW_REGION_A_PERCENT", "50"))

    def probe(self, scope: str) -> HttpMeasurement:
        """Controller-owned HTTP observation over the external (edge) path."""
        started = time.time()
        region, status = None, None
        try:
            response = self.client.get(f"{self.edge}/http/{scope}", timeout=2)
            status = response.status_code
            body = response.json()
            region = body.get("region") if isinstance(body, dict) else None
        except (httpx.HTTPError, ValueError):
            pass
        return HttpMeasurement(uuid.uuid4().hex, started, time.time(), region, status)

    def recovery_window(self, action_id: str, destination: str) -> RecoveryWindow:
        started = time.time()
        requests = []
        while len(requests) < 12 or time.time() - started < 11:
            requests.append(self.probe("cloudfront"))
            time.sleep(0.9)
        return RecoveryWindow(action_id, started, tuple(requests), self.probe(destination))

    def tick(self) -> dict:
        now = time.time()
        try:
            assessment = self.client.get(f"{self.ml}/v1/assessment", timeout=10)
            snapshot = self.client.get(f"{self.ml}/v1/snapshot", timeout=10)
            if assessment.status_code != 200 or snapshot.status_code != 200:
                return {"at": now, "state": "waiting_for_telemetry"}
            assessment, snapshot = assessment.json(), snapshot.json()
        except (httpx.HTTPError, ValueError) as exc:
            return {"at": now, "state": "ml_unreachable", "error": str(exc)}
        try:
            weight = self.route.read_weight() if self.route else self.weight_fallback
        except OSError:
            return {"at": now, "state": "route_read_failed"}
        proposal = build_proposal(assessment, self.group, weight)
        hpa = {r: emulated_hpa(r, snapshot, now) for r in REGIONS}
        decision = decide(snapshot, proposal, current_region_a_percent=weight, hpa_observations=hpa, now=now)
        record = {"at": now, "group": self.group, "controller_mode": self.mode,
                  "region_a_percent": weight, "condition": assessment["detectors"][self.group]["condition"],
                  "all_groups": {g: d.get("condition") for g, d in assessment["detectors"].items()},
                  "proposal": proposal, "decision": decision["decision"], "reasons": decision["reasons"],
                  "plan": decision.get("plan"), "executed_locally": None}
        if decision["decision"] == "failover" and self.safety is not None:
            record["executed_locally"] = self.execute_failover(snapshot, decision["proposal"], decision["plan"])
        elif decision["decision"] == "scale":
            record["executed_locally"] = {"state": "not_executed",
                                          "reason": "eks_scale_requires_approve_live_write"}
        return record

    def execute_failover(self, snapshot: dict, proposal: dict, plan: dict) -> dict:
        source, destination = plan["from_region"], plan["to_region"]
        sample = self.probe(source)
        degraded = not sample.met_deadline() or sample.region != source
        dest = self.probe(destination)
        result = self.safety.step(snapshot, proposal, observed_at=sample.completed_at,
                                  source_degraded=degraded, destination_responsive=dest.met_deadline())
        out = {"step": asdict(result), "source_probe_ms": round((sample.completed_at - sample.started_at) * 1000, 1),
               "source_probe_status": sample.status_code}
        if result.state == "verifying" and result.action_id:
            verified = self.safety.verify(self.recovery_window(result.action_id, destination))
            out["verify"] = asdict(verified)
        return out


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    mode = os.environ.get("DEMO_CONTROLLER_MODE", "local")
    if mode not in ("local", "review", "disabled"):
        raise SystemExit("DEMO_CONTROLLER_MODE must be local, review or disabled")
    data = Path(os.environ.get("DEMO_DATA_DIR", "/app/data"))
    interval = float(os.environ.get("DECISION_INTERVAL_SEC", "5"))
    with httpx.Client(trust_env=False) as client:
        loop = DemoLoop(client=client, ml_url=os.environ["ML_URL"], edge_url=os.environ.get("EDGE_URL", ""),
                        gateway_url=os.environ.get("GATEWAY_URL", "http://127.0.0.1:8000"),
                        token=os.environ.get("LAB_TOKEN", ""), data_dir=data,
                        mode="review" if mode == "disabled" else mode,
                        group=os.environ.get("DEMO_DECISION_GROUP", "aws_ripe_http"))
        while True:
            started = time.monotonic()
            if mode != "disabled":
                record = loop.tick()
                loop.log.write(record)
                log.info("%s %s %s", record.get("condition"), record.get("decision", record.get("state")),
                         (record.get("executed_locally") or {}).get("step", {}).get("state", ""))
            time.sleep(max(0.0, interval - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
