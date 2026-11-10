"""Model proposal -> policy gates -> actuation -> recovery verification -> rollback.

Modes: shadow (review + audit only), dry_run (full plan, no writes), enforce.
Model output is untrusted; review_proposal() is re-run here against LIVE actuator state.
"""
from __future__ import annotations
from dataclasses import dataclass
import json
from pathlib import Path
import time
import uuid
from typing import Callable

from ..ingestion.decisions import review_proposal
from .actuators import Actuator
from .audit import AuditLog

def _json_safe(value):
    """Model output is untrusted: keep a record of it even if it can't be stored as strict JSON."""
    try:
        json.dumps(value, allow_nan=False)
        return value
    except (TypeError, ValueError):
        return {"unserializable_repr": repr(value)[:500]}

def classify_health(s: dict | None, p: "Policy") -> str:
    if not s or s.get("status") != "ok" or s.get("age_seconds", 10**9) > p.max_probe_age_s or s.get("samples", 0) < 3:
        return "unknown"
    if s.get("error_ratio", 1) >= p.degraded_error_ratio:
        return "degraded"
    if s.get("p95_ms") is None:  # latency not measured: unknown, so a telemetry gap can't trigger a rollback
        return "unknown"
    return "degraded" if s["p95_ms"] >= p.degraded_latency_ms else "healthy"

@dataclass
class Policy:
    mode: str = "shadow"                 # shadow | dry_run | enforce
    cooldown_s: float = 300
    max_actions_per_hour: int = 4
    max_replicas: int = 6
    max_scale_step: int = 2
    headroom_cpu_pct: float = 70         # shifting onto a destination hotter than this scales it first
    degraded_latency_ms: float = 800
    degraded_error_ratio: float = .20
    max_probe_age_s: float = 90
    actuator_settle_s: float = 0         # time a route change takes to reach clients (0 for the local gateway;
                                         # for weighted DNS use at least the record TTL plus resolver caching)
    verify_timeout_s: float = 90
    verify_interval_s: float = 5
    verify_consecutive: int = 3          # consecutive healthy probes required to call it recovered
    kill_switch_file: str | None = None  # existence of this file disables all actuation
    input_mode: str = "combined"         # internal_only accepts snapshots without external telemetry

class Mitigator:
    def __init__(self, actuator: Actuator, probe: Callable[[], dict], audit: AuditLog,
                 policy: Policy | None = None, clock=time.time, sleep=time.sleep):
        self.act, self.probe, self.audit = actuator, probe, audit
        self.policy, self.clock, self.sleep = policy or Policy(), clock, sleep

    # ---- planning -------------------------------------------------------------
    def _kill_switch_engaged(self) -> bool:
        return bool(self.policy.kill_switch_file and Path(self.policy.kill_switch_file).exists())

    def _gates(self, key: str) -> str | None:
        p, now = self.policy, self.clock()
        if p.mode not in ("shadow", "dry_run", "enforce"):
            return "invalid_mode"
        if self._kill_switch_engaged():
            return "kill_switch_engaged"
        try:
            return self._history_gates(key, now)
        except Exception:  # safety state unknown -> never act blind
            return "audit_unreadable"

    def _history_gates(self, key: str, now: float) -> str | None:
        p = self.policy
        recent = self.audit.actions_since(now - 3600)
        # Acting again before the last change has reached clients is how a slow actuator oscillates.
        if recent and now - max(r["ts"] for r in recent) < max(p.cooldown_s, p.actuator_settle_s):
            return "cooldown"
        if len(recent) >= p.max_actions_per_hour:
            return "rate_limit"
        if any(r.get("idempotency_key") == key for r in self.audit.read() if r.get("event") == "step_applied"):
            return "duplicate_proposal"
        return None

    def _plan(self, snapshot: dict, proposal: dict, review_kwargs: dict) -> list[dict]:
        if proposal["action"] == "scale_out":
            return [{"kind": "scale_out", "region": proposal["region"], "target": proposal["target_replicas"]}]
        target, cur = proposal["target_region_a_percent"], review_kwargs["current_region_a_percent"]
        dest = "region-b" if target < cur else "region-a"
        steps = []
        cpu = snapshot["regions"][dest]["features"].get("cpu_busy_pct")
        replicas = review_kwargs["current_replicas"].get(dest)
        if self.act.can_scale and replicas and cpu is not None and cpu > self.policy.headroom_cpu_pct \
                and replicas < self.policy.max_replicas:
            steps.append({"kind": "scale_out", "region": dest, "target": min(replicas + 1, self.policy.max_replicas),
                          "reason": "destination_headroom"})
        steps.append({"kind": "shift_traffic", "destination": dest, "target": target})
        return steps

    # ---- one control cycle ----------------------------------------------------
    def run_cycle(self, snapshot: dict, proposal: dict) -> dict:
        cycle = uuid.uuid4().hex[:12]
        action = proposal.get("action") if isinstance(proposal, dict) else None
        out = {"cycle_id": cycle, "snapshot_id": snapshot.get("snapshot_id"), "mode": self.policy.mode,
               "requested_action": _json_safe(action), "steps": []}
        def finish(status, **kw):
            out.update(status=status, **kw)
            try:  # the outcome must still reach the caller if the log is unavailable
                self.audit.append("cycle_end", cycle_id=cycle, status=status, snapshot_id=out["snapshot_id"],
                                  requested_action=out["requested_action"], detail=kw)
            except Exception as exc:
                out["audit_error"] = type(exc).__name__
            return out
        try:
            self.audit.append("proposal_received", cycle_id=cycle, snapshot_id=out["snapshot_id"], proposal=_json_safe(proposal))
        except Exception as exc:  # no audit trail -> no action
            return finish("blocked", reason="audit_unavailable", error=type(exc).__name__)
        if action in ("hold", "investigate"):
            return finish("held")
        try:
            route = self.act.read_route()
        except Exception as exc:  # cannot see live state -> never act blind
            return finish("blocked", reason="actuator_state_unreadable", error=type(exc).__name__, detail=str(exc)[:200])
        # A scaling problem must not block rerouting: unknown capacity only rules out scaling that region.
        replicas, replica_errors = {}, {}
        if self.act.can_scale:
            for r in ("region-a", "region-b"):
                try:
                    replicas[r] = self.act.read_capacity(r)
                except Exception as exc:
                    replicas[r], replica_errors[r] = None, type(exc).__name__
        if replica_errors:
            out["replica_read_errors"] = replica_errors
        kwargs = {"current_region_a_percent": route, "current_replicas": replicas}
        review = review_proposal(snapshot, proposal, scale_adapter_available=self.act.can_scale,
                                 max_replicas=self.policy.max_replicas, max_scale_step=self.policy.max_scale_step,
                                 input_mode=self.policy.input_mode, **kwargs)
        out["review"] = review
        if not review["eligible_for_operator_review"]:
            return finish("rejected", reasons=review["rejection_reasons"])
        key = f'{snapshot["snapshot_id"]}:{proposal["action"]}:{proposal.get("target_region_a_percent", proposal.get("target_replicas"))}'
        blocked = self._gates(key)
        if blocked:
            return finish("blocked", reason=blocked)
        plan = self._plan(snapshot, proposal, kwargs)
        out["plan"] = plan
        if self.policy.mode != "enforce":
            return finish("shadow" if self.policy.mode == "shadow" else "dry_run", plan=plan)
        for step in plan:
            if self._kill_switch_engaged():  # may be engaged while an earlier step was verifying
                return finish("halted", reason="kill_switch_engaged", plan=plan)
            result = self._execute(cycle, key, step)
            out["steps"].append(result)
            if result["outcome"] != "recovered":
                return finish(result["outcome"], plan=plan)
        return finish("recovered", plan=plan)

    # ---- actuation + verification --------------------------------------------
    def _read(self, step):
        return self.act.read_route() if step["kind"] == "shift_traffic" else self.act.read_replicas(step["region"])

    def _write(self, step, value):
        if step["kind"] == "shift_traffic":
            self.act.set_route(value)
        else:
            self.act.set_replicas(step["region"], value)

    def _watched(self, step) -> str:
        return step["destination"] if step["kind"] == "shift_traffic" else step["region"]

    def _execute(self, cycle, key, step) -> dict:
        res = {"step": step}
        try:
            before_state, before_health = self._read(step), self.probe()
            self.audit.append("step_applied", cycle_id=cycle, idempotency_key=key, step=step,
                              previous=before_state, baseline_health=before_health)  # logged BEFORE the write
        except Exception as exc:  # nothing has been written yet
            res.update(outcome="aborted", error=type(exc).__name__)
            return res
        res["previous"] = before_state
        try:
            self._write(step, step["target"])
            if self._read(step) != step["target"]:
                raise RuntimeError("read-back mismatch")
        except Exception as exc:
            res["error"] = type(exc).__name__
            return self._rollback(cycle, step, before_state, res, "apply_failed")
        verdict, history = self._verify(step)
        res["verification"] = {"verdict": verdict, "samples": history}
        try:
            self.audit.append("verification", cycle_id=cycle, step=step, verdict=verdict, samples=history)
        except Exception as exc:  # a change we can't record is not one we can call successful
            res["audit_error"] = type(exc).__name__
            verdict = "audit_unavailable"
        if verdict == "recovered":
            res["outcome"] = "recovered"
            return res
        if step["kind"] == "scale_out":
            # Extra capacity is bounded by max_replicas and cheap to keep; removing it mid-incident is riskier.
            res.update(outcome="unverified", note="scale-out kept; operator must confirm and scale down")
            return res
        return self._rollback(cycle, step, before_state, res, verdict)

    def _observe(self, step, region) -> str:
        try:
            state = classify_health(self.probe().get(region), self.policy)
            if state == "healthy" and step["kind"] == "scale_out" and self.act.read_ready_replicas(region) < step["target"]:
                return "pods_not_ready"
            return state
        except Exception:  # a failed probe proves nothing: keep waiting, and let the timeout roll back
            return "unknown"

    def _verify(self, step) -> tuple[str, list[str]]:
        p, region, streak, hist = self.policy, self._watched(step), 0, []
        if step["kind"] == "shift_traffic" and p.actuator_settle_s > 0:
            self.sleep(p.actuator_settle_s)  # health seen before the change has propagated says nothing about it
        deadline = self.clock() + p.verify_timeout_s
        while True:
            state = self._observe(step, region)
            hist.append(state)
            streak = streak + 1 if state == "healthy" else 0
            if streak >= p.verify_consecutive:
                return "recovered", hist
            if state == "degraded" and step["kind"] == "shift_traffic":
                return "destination_degraded", hist  # we moved load onto a bad region: undo fast
            if self.clock() >= deadline:
                return "verification_timeout", hist
            self.sleep(p.verify_interval_s)

    def _rollback(self, cycle, step, previous, res, why) -> dict:
        try:
            self._write(step, previous)
            ok = self._read(step) == previous
        except Exception:
            ok = False
        res["outcome"] = "rolled_back" if ok else "rollback_failed"
        res["rollback_reason"] = why
        try:
            self.audit.append("rollback", cycle_id=cycle, step=step, reason=why, restored=previous, ok=ok)
        except Exception as exc:
            res["audit_error"] = type(exc).__name__
        return res
