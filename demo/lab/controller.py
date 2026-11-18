"""Demo decision loop: ML assessment -> proposal -> safety review -> EKS decision.

Every tick:
1. read the shadow assessment from the ML service;
2. turn the selected feature group's detector result into an
   ``adaptivenet.proposal.v1`` (untrusted model output);
3. ask ``adaptivenet.controller.end_to_end.decide`` for a dry-run EKS decision
   (scale / failover / no-op) behind the independent safety reviewer;
4. execute only a ``failover``, only on the lab gateway, through
   ``SafetyController(mode="local")``, and only once the confidence rules agree.
   Those rules read raw measurements, never the ML output. ``scale`` is shown,
   never executed; live HPA writes need ``scripts/end_to_end.py execute --approve-live-write``.

``DEMO_CONTROLLER_MODE``: ``local`` (default, actuates the lab gateway),
``review`` (decisions only; used for replay), or ``disabled``.
Must share the gateway's network namespace: the route adapter is loopback-only.
"""
from __future__ import annotations

from dataclasses import asdict, replace
import json
import logging
import os
from pathlib import Path
import time
import uuid

import httpx

from adaptivenet.controller.confidence import (
    RIPE_TOTAL_LOSS_RULE, RULES_VERSION, ConfidenceRules, ConfidenceTracker, Verdict,
)
from adaptivenet.controller.end_to_end import ALLOWLIST, decide
from adaptivenet.controller.journal import ActionJournal
from adaptivenet.controller.local_gateway import GatewayRoute
from adaptivenet.controller.recovery import HttpMeasurement, RecoveryWindow, DEADLINE_SEC, evaluate_recovery
from adaptivenet.ingestion.decisions import review_proposal
from adaptivenet.controller.safety import SafetyController
from adaptivenet.controller.scaling import HpaObservation

from .paths import ObserverPaths

log = logging.getLogger("demo.controller")
REGIONS = ("region-a", "region-b")
FAILOVER_STEP = 25
SHIFT_CONDITIONS = ("network_degradation", "application_failure", "region_down")
MAX_LOG_BYTES = 2 * 1024 * 1024
# Failback: return traffic to the primary (Singapore) only after it has looked
# healthy on all signals for FAILBACK_HEALTHY_SEC (confidence rule B1), one
# 25-point step at a time, then FAILBACK_CHECKS controller probes.
FAILBACK_CHECKS = 3
BASELINE_MIN_REPLICAS = 2


def confidence_rules(env=os.environ) -> ConfidenceRules:
    rules = ConfidenceRules.from_env(env)
    if "FAILBACK_HEALTHY_SEC" in env:  # older lab setting, kept working
        rules = replace(rules, failback_healthy_sec=float(env["FAILBACK_HEALTHY_SEC"]))
    return rules


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
        self.client, self.ml = client, ml_url.rstrip("/")
        self.paths = ObserverPaths()
        self.mode, self.group = mode, group
        self.data_dir = data_dir
        self.log = DecisionLog(data_dir / "decisions.jsonl")
        self.route = None
        self.safety = None
        if mode == "local":
            self.route = GatewayRoute(httpx.Client(base_url=gateway_url, timeout=3, trust_env=False), token)
            self._new_controller()
        # Review mode has no gateway to read; assume an even split unless told otherwise.
        self.weight_fallback = int(os.environ.get("DEMO_REVIEW_REGION_A_PERCENT", "50"))
        self.confidence = ConfidenceTracker(confidence_rules())
        self.failback_checks = 0
        self.failback_started = False

    def _new_controller(self, *, archive: bool = False) -> None:
        path = self.data_dir / "controller-journal.db"
        if archive and path.exists():
            # Keep the finished incident's journal for audit; start a fresh one.
            stamp = time.strftime("%Y%m%dT%H%M%S")
            for suffix in ("", "-wal", "-shm"):
                old = self.data_dir / f"controller-journal.db{suffix}"
                if old.exists():
                    os.replace(old, self.data_dir / f"controller-journal-{stamp}.db{suffix}")
        journal = ActionJournal(path)
        self.safety = SafetyController(journal, self.route, mode="local")

    def maybe_reset(self) -> bool:
        """Operator demo reset (requested via the view): start a fresh journal.

        Only the local lab journal is discarded; it records lab gateway steps only.
        """
        request = self.data_dir / "reset-request"
        if not request.exists():
            return False
        request.unlink(missing_ok=True)
        if self.route is not None:
            for suffix in ("", "-wal", "-shm"):
                (self.data_dir / f"controller-journal.db{suffix}").unlink(missing_ok=True)
            self._new_controller()
        self.log.write({"at": time.time(), "state": "demo_reset", "controller_mode": self.mode})
        return True

    def probe(self, scope: str) -> HttpMeasurement:
        """Controller-owned HTTP observation over the external (edge) path."""
        started = time.time()
        region, status = None, None
        try:
            url, headers = self.paths.http(scope)
            response = self.client.get(url, headers=headers, timeout=2)
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
        # Controller-owned client probes are the third confidence signal. Review
        # mode (replay) has no lab path that matches the data, so HTTP is unknown.
        samples = {r: self.probe(r) for r in REGIONS} if self.route is not None else {}
        http_ok = {r: s.met_deadline() and s.region == r for r, s in samples.items()}
        self.confidence.observe(snapshot, now=now, http_ok=http_ok)
        proposal = build_proposal(assessment, self.group, weight)
        hpa = {r: emulated_hpa(r, snapshot, now) for r in REGIONS}
        decision = decide(snapshot, proposal, current_region_a_percent=weight, hpa_observations=hpa, now=now)
        record = {"at": now, "group": self.group, "controller_mode": self.mode,
                  "region_a_percent": weight, "condition": assessment["detectors"][self.group]["condition"],
                  "all_groups": {g: d.get("condition") for g, d in assessment["detectors"].items()},
                  "proposal": proposal, "decision": decision["decision"], "reasons": decision["reasons"],
                  "plan": decision.get("plan"), "executed_locally": None,
                  "confidence": self.confidence_record(now)}
        if decision["decision"] == "failover":
            verdict = self.confidence.failover(decision["plan"]["from_region"], now)
            record["confidence"]["failover"] = asdict(verdict)
            if self.safety is not None:
                record["executed_locally"] = self.execute_failover(
                    snapshot, decision["proposal"], decision["plan"], confident=verdict, samples=samples)
                self.note_traffic_change(weight, towards=decision["plan"]["to_region"])
            elif not verdict.ok:
                record["executed_locally"] = {"step": {"state": "confirming_signals", "reasons": [verdict.reason]}}
        elif decision["decision"] == "scale":
            verdict = self.confidence.scale_up(decision["plan"]["region"], now)
            record["confidence"]["scale_up"] = asdict(verdict)
            record["executed_locally"] = {"state": "not_executed",
                                          "reason": "eks_scale_requires_approve_live_write"
                                          if verdict.ok else verdict.reason}
        failback = self.failback(snapshot, assessment, weight, decision["decision"], now)
        if failback is not None:
            record.update(failback)
        return record

    def confidence_record(self, now: float) -> dict:
        out = {"rules_version": RULES_VERSION, "ripe_extra_rule": RIPE_TOTAL_LOSS_RULE,
               "rules": asdict(self.confidence.rules), "regions": self.confidence.summary()}
        # Scale-down is a recommendation only; the lab HPA always sits at its baseline.
        out["scale_down"] = {r: asdict(self.confidence.scale_down(
            r, now, current_min=BASELINE_MIN_REPLICAS, baseline_min=BASELINE_MIN_REPLICAS)) for r in REGIONS}
        return out

    def note_traffic_change(self, before: int, *, towards: str) -> None:
        try:
            if self.route is not None and self.route.read_weight() != before:
                self.confidence.record_traffic_change(time.time(), towards=towards)
        except OSError:
            pass

    def failback(self, snapshot: dict, assessment: dict, weight: int, decided: str, now: float) -> dict | None:
        """Gated, stepped return of traffic to Singapore (region-a) once it is healthy again.

        Gates: confidence rules B1/B2 (Singapore has no bad signal and at
        least two good ones for 120 s, and 120 s since the last failover
        step); no failover decision this tick; the independent safety reviewer approves a
        25-point shift towards Singapore; three consecutive controller probes of
        Singapore meet the client deadline; then real client requests are checked
        and the step is rolled back if Singapore does not serve them in time.
        """
        if self.safety is None or self.route is None:
            return None
        if weight >= 100 or decided != "no-op":
            self.failback_checks = 0
            if weight >= 100:
                self.failback_started = False
            return None
        verdict = self.confidence.failback(now)
        if not verdict.ok and verdict.reason.startswith("primary_not_clean"):
            self.failback_checks = 0
            return None
        target = min(100, weight + 25)
        base = {"decision": "failback", "proposal": {"action": "shift_traffic", "target_region_a_percent": target,
                                                      "affected_region": "region-b", "failback": True}}
        if not verdict.ok:
            return {**base, "reasons": ["primary_must_stay_healthy"],
                    "executed_locally": {"step": {"state": "waiting_healthy", "reasons": [verdict.reason]}}}
        waited = now - self.confidence.regions["region-a"].healthy_since
        proposal = {"contract_version": "adaptivenet.proposal.v1", "snapshot_id": snapshot.get("snapshot_id"),
                    "action": "shift_traffic", "target_region_a_percent": target, "confidence": 0.9,
                    "model_id": f"failback:{self.group}", "affected_region": "region-b",
                    "evidence": [f"primary_healthy_for_{int(waited)}s", f"feature_group:{self.group}"]}
        review = review_proposal(snapshot, proposal, current_region_a_percent=weight)
        if not review["eligible_for_operator_review"]:
            self.failback_checks = 0
            return {**base, "reasons": review["rejection_reasons"],
                    "executed_locally": {"step": {"state": "blocked", "reasons": review["rejection_reasons"]}}}
        sample = self.probe("region-a")
        self.failback_checks = self.failback_checks + 1 if sample.met_deadline() and sample.region == "region-a" else 0
        if self.failback_checks < FAILBACK_CHECKS:
            return {**base, "reasons": ["reviewer_eligible_shift"],
                    "executed_locally": {"step": {"state": "qualifying", "qualifying_count": self.failback_checks}}}
        if not self.failback_started:
            # The failover incident is over; its journal is archived so the next
            # incident starts clean and manual history is never overwritten.
            self._new_controller(archive=True)
            self.failback_started = True
        self.failback_checks = 0
        try:
            self.route.request_weight(original=weight, target=target)
            if self.route.read_weight() != target:
                raise OSError("route read-back mismatch")
        except OSError as exc:
            return {**base, "reasons": ["route_request_failed"],
                    "executed_locally": {"step": {"state": "blocked", "reasons": [str(exc)]}}}
        recorded = time.time()
        window = self.recovery_window(uuid.uuid4().hex, "region-a")
        state, reason = evaluate_recovery(window, original_weight=weight, target_weight=target,
                                          recorded_at=recorded, now=time.time(), timeout_sec=30)
        if state == "blocked":
            try:
                self.route.request_weight(original=target, target=weight)
            except OSError:
                reason += "; rollback_failed"
            self.confidence.record_traffic_change(time.time(), towards="region-b")
            self.confidence.restart_failback_clock()
            return {**base, "reasons": [reason],
                    "executed_locally": {"verify": {"state": "rolled_back", "reasons": [reason]}}}
        self.confidence.record_traffic_change(time.time(), towards="region-a")
        return {**base, "reasons": ["reviewer_eligible_shift"], "region_a_percent": target,
                "executed_locally": {"verify": {"state": "failback_step_ok", "reasons": [reason]}}}

    def execute_failover(self, snapshot: dict, proposal: dict, plan: dict, *,
                         confident: Verdict | None = None, samples: dict | None = None) -> dict:
        """Run one SafetyController step. Without confidence the step still runs
        (it reconciles pending work and resets its own 3-probe streak) but is
        told the source is not degraded, so it can never move traffic."""
        source, destination = plan["from_region"], plan["to_region"]
        samples = samples or {}
        sample = samples.get(source) or self.probe(source)
        degraded = not sample.met_deadline() or sample.region != source
        if confident is not None and not confident.ok:
            degraded = False
        dest = samples.get(destination) or self.probe(destination)
        result = self.safety.step(snapshot, proposal, observed_at=sample.completed_at,
                                  source_degraded=degraded, destination_responsive=dest.met_deadline())
        step = asdict(result)
        if confident is not None and not confident.ok and result.state == "observing":
            step = {**step, "state": "confirming_signals", "reasons": [confident.reason]}
        out = {"step": step, "source_probe_ms": round((sample.completed_at - sample.started_at) * 1000, 1),
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
                loop.maybe_reset()
                record = loop.tick()
                loop.log.write(record)
                log.info("%s %s %s", record.get("condition"), record.get("decision", record.get("state")),
                         (record.get("executed_locally") or {}).get("step", {}).get("state", ""))
            time.sleep(max(0.0, interval - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
