#!/usr/bin/env python3
"""Offline replay of the 20-trial RIPE campaign through the controller's ACTION path.

Real recorded inputs only (no AWS calls, nothing is changed anywhere):
  AMP export (amp-region-*.jsonl), RIPE ping results (ripe/<msm>.json), the HTTP
  witness (witness.stdout, kind=http_witness), the campaign plan/log and the guarded
  trial records (SSM-confirmed ground truth).

Every 15 s tick (the witness cadence) on a replayed clock:
  1. features.v1 snapshot = ingestion.features.make_snapshot(observations up to t)
     (RIPE parsed with the real ingestion parser, parse_atlas_latest)
  2. confidence rules (controller/confidence.py) observe the snapshot + the witness
     result for each region (HTTP 200, served by that region, <= 2 s)
  3. a proposal from one of two detectors (both unchanged, as committed):
       campaign : the frozen campaign detector (ml/team_starter/ripe_value.py,
                  AWS-only fair rule OR primary RIPE rule), mapped to a 25-point shift
       lab      : the demo lab rule detector for aws_ripe_http (demo/lab/detectors.py)
                  + demo/lab/controller.build_proposal, as the lab controller does
  4. end_to_end.decide (safety reviewer, unchanged)
  5. confidence failover verdict, then SafetyController(mode="local").step with the
     witness sample as the controller-owned observation (unchanged, 3 fresh checks)
An ACTION is the first tick where the SafetyController reserves and applies a
25-point route step (state "verifying"). The replay stops a slot there: client
recovery after a move cannot be replayed, because traffic never moved in reality.
The route starts at 50 % / 50 % so a move is possible away from either region.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "services"), str(ROOT)]

from adaptivenet.controller.confidence import ConfidenceTracker, ConfidenceRules  # noqa: E402
from adaptivenet.controller.end_to_end import decide  # noqa: E402
from adaptivenet.controller.journal import ActionJournal  # noqa: E402
from adaptivenet.controller.safety import SafetyController  # noqa: E402
from adaptivenet.ingestion.config import RegionConfig  # noqa: E402
from adaptivenet.ingestion.fast_signals import build_fast_signals  # noqa: E402
from adaptivenet.ingestion.features import ATLAS_FRESH_SEC, make_snapshot  # noqa: E402
from adaptivenet.ingestion.handoff import build_model_input  # noqa: E402
from adaptivenet.ingestion.models import Observation  # noqa: E402
from adaptivenet.ingestion.sources import parse_atlas_latest  # noqa: E402
from ml.team_starter import ripe_value as rv  # noqa: E402

REGIONS = ("region-a", "region-b")
OTHER = {"region-a": "region-b", "region-b": "region-a"}
AMP_METRICS = ("app_up", "http_rps", "http_5xx_rps", "http_p95_ms", "cpu_busy_pct",
               "memory_available_bytes", "memory_total_bytes", "node_load1")
TICK_SEC = 15
WARMUP_SEC = 600
HTTP_DEADLINE_MS = 2000
STEP = 25
STAGES = ("no_proposal", "reviewer", "confidence", "safety_controller", "action")


def ts(value) -> float:
    return value if isinstance(value, (int, float)) else datetime.fromisoformat(value).timestamp()


# ---------------------------------------------------------------- inputs
class Inputs:
    def __init__(self, campaign: Path):
        self.dir = campaign
        self.plan = json.loads((campaign / "campaign-plan.json").read_text())
        log = [json.loads(l) for l in (campaign / "campaign-log.jsonl").read_text().splitlines() if l.strip()]
        self.slots = rv.campaign_slots(self.plan, log, campaign / "trials")
        self.amp = {r: [json.loads(l) for l in (campaign / f"amp-{r}.jsonl").read_text().splitlines() if l.strip()]
                    for r in REGIONS}
        self.msm = {}
        self.ripe_raw = {}
        for slot in self.plan["slots"]:
            self.msm.setdefault(slot["region"], slot["ripe_measurement_id"])
        for region, msm in self.msm.items():
            records = json.loads((campaign / "ripe" / f"{msm}.json").read_text())
            self.ripe_raw[region] = sorted(records, key=lambda r: r["timestamp"])
        self.witness = []
        for line in (campaign / "witness.stdout").read_text().splitlines():
            if '"http_witness"' in line:
                try:
                    self.witness.append(json.loads(line))
                except ValueError:
                    continue
        self.witness.sort(key=lambda r: ts(r["timestamp"]))
        self.index = rv.ProbeIndex(rv.load_samples(
            {r: [campaign / "ripe" / f"{m}.json"] for r, m in self.msm.items()}))
        self.impaired = {r: [(s["request_unix"], s["request_unix"] + s["duration_seconds"])
                             for s in self.slots if s["effect_confirmed"] and s["region"] == r] for r in REGIONS}

    def observations(self, now: float) -> list[Observation]:
        events = []
        for region in REGIONS:
            for row in self.amp[region]:
                if now - 180 <= row["ts"] <= now:
                    for metric in AMP_METRICS:
                        value = row.get(metric)
                        if isinstance(value, (int, float)):
                            events.append(Observation(region, "aws_amp", metric, float(value), "x",
                                                      row["ts"], row["ts"], f"amp-{region}"))
            past = [r for r in self.ripe_raw[region] if now - ATLAS_FRESH_SEC <= r["timestamp"] <= now]
            cfg = RegionConfig(region, "", "", self.msm[region])
            events.extend(parse_atlas_latest(past, cfg, now))
        return events

    def witness_events(self, now: float) -> list[dict]:
        return [w for w in self.witness if now - 900 <= ts(w["timestamp"]) <= now]

    def witness_sample(self, region: str, now: float) -> dict | None:
        """Latest direct witness request to ``region`` in the last 45 s."""
        for w in reversed(self.witness_events(now)):
            if w.get("scope") == region:
                return w if now - ts(w["timestamp"]) <= 45 else None
        return None


def http_ok(sample: dict | None, region: str) -> bool | None:
    if sample is None:
        return None
    latency = sample.get("latency_ms")
    return (sample.get("http_status") == 200 and sample.get("origin") == region
            and isinstance(latency, (int, float)) and latency <= HTTP_DEADLINE_MS)


# ---------------------------------------------------------------- proposals
def shift_proposal(snapshot: dict, region: str, weight: int, model_id: str, evidence: list[str]) -> dict:
    target = max(0, min(100, weight - STEP if region == "region-a" else weight + STEP))
    return {"contract_version": "adaptivenet.proposal.v1", "snapshot_id": snapshot["snapshot_id"],
            "action": "shift_traffic", "target_region_a_percent": target, "confidence": 0.85,
            "model_id": model_id, "affected_region": region,
            "evidence": ["shadow_condition:network_degradation", f"affected_region:{region}", *evidence]}


def hold(snapshot: dict, model_id: str) -> dict:
    return {"contract_version": "adaptivenet.proposal.v1", "snapshot_id": snapshot["snapshot_id"],
            "action": "hold", "confidence": 0.9, "model_id": model_id, "evidence": ["shadow_condition:healthy"]}


def campaign_proposal(inputs: Inputs, snapshot: dict, now: float, weight: int) -> dict:
    """Frozen campaign detector: AWS-only fair rule OR primary RIPE rule, per region."""
    alerts = {}
    for region in REGIONS:
        rows = [r for r in inputs.amp[region] if r["ts"] <= now]
        latest = rows[-1] if rows and now - rows[-1]["ts"] <= 180 else None
        hist = [r.get("http_p95_ms") for r in rows if now - rv.BASELINE_SECONDS <= r["ts"] < now
                and not any(a <= r["ts"] <= b for a, b in inputs.impaired[region])]
        aws = None
        if latest is not None:
            values = {f"{region}.{k}": v for k, v in latest.items() if k not in ("ts", "region")}
            aws = rv.aws_view_alert(values, True, region, hist)
        ripe = rv.ripe_path_alert(rv.path_features(inputs.index, region, now), "primary")
        alerts[region] = bool(aws) or bool(ripe)
    bad = [r for r in REGIONS if alerts[r]]
    if len(bad) != 1:
        return hold(snapshot, "campaign_frozen_detector")
    return shift_proposal(snapshot, bad[0], weight, "campaign_frozen_detector", ["frozen_aws_or_ripe_primary"])


def lab_proposal(inputs: Inputs, snapshot: dict, now: float, weight: int) -> dict:
    from demo.lab.controller import build_proposal
    from demo.lab.detectors import assess_group
    # Same shape as demo/lab/ml_service.detection_input.
    payload = {"contract_version": "adaptivenet.detection_input.v1", "generated_at_unix": now,
               "slow": build_model_input(snapshot, mode="combined", now=now),
               "fast": build_fast_signals(inputs.witness_events(now), now=now)}
    detector = assess_group(payload, "aws_ripe_http")
    if detector.get("error"):
        raise RuntimeError(f"lab detector input rejected: {detector['error']}")
    assessment = {"snapshot_id": snapshot["snapshot_id"], "detectors": {"aws_ripe_http": detector}}
    return build_proposal(assessment, "aws_ripe_http", weight)


PROPOSERS = {"campaign": campaign_proposal, "lab": lab_proposal}


# ---------------------------------------------------------------- replay
class Route:
    def __init__(self, weight: int):
        self.weight = weight

    def read_weight(self) -> int:
        return self.weight

    def request_weight(self, *, original: int, target: int) -> None:
        if original != self.weight:
            raise OSError("route changed")
        self.weight = target


@contextmanager
def replay_clock():
    clock = {"now": 0.0}
    with mock.patch("time.time", lambda: clock["now"]):
        yield clock


def replay_slot(inputs: Inputs, slot: dict, proposer: str, *, start_weight: int = 50,
                rules: ConfidenceRules | None = None) -> dict:
    start = slot["request_unix"] - WARMUP_SEC
    end = slot["request_unix"] + slot["duration_seconds"]
    tracker = ConfidenceTracker(rules)
    route = Route(start_weight)
    stages: dict[str, Counter] = {s: Counter() for s in STAGES}
    first = {"campaign_detector_or_lab_proposal": None, "reviewer_pass": None, "confident": None}
    action = None
    # Which region each shift proposal blamed, and the impaired region's signal votes.
    proposals = {"warmup_before_fault": Counter(), "fault_window": Counter()}
    votes = {sig: Counter() for sig in ("aws", "ripe", "http")}
    with tempfile.TemporaryDirectory() as tmp, replay_clock() as clock:
        safety = SafetyController(ActionJournal(Path(tmp) / "journal.db"), route, mode="local")
        t = start
        while t <= end and action is None:
            clock["now"] = t
            snapshot = make_snapshot(inputs.observations(t), t)
            samples = {r: inputs.witness_sample(r, t) for r in REGIONS}
            tracker.observe(snapshot, now=t, http_ok={r: http_ok(samples[r], r) for r in REGIONS})
            weight = route.read_weight()
            proposal = PROPOSERS[proposer](inputs, snapshot, t, weight)
            decision = decide(snapshot, proposal, current_region_a_percent=weight, now=t)
            in_window = t >= slot["request_unix"]
            if proposal.get("action") == "shift_traffic":
                proposals["fault_window" if in_window else "warmup_before_fault"][proposal.get("affected_region")] += 1
            if in_window:
                for sig, value in tracker.regions[slot["region"]].votes.items():
                    votes[sig][value] += 1
            if proposal.get("action") != "shift_traffic":
                stage, reason = "no_proposal", proposal.get("action")
            elif decision["decision"] != "failover":
                stage, reason = "reviewer", ",".join(decision["reasons"])
            else:
                source = decision["plan"]["from_region"]
                verdict = tracker.failover(source, t)
                sample, dest = samples[source], samples[OTHER[source]]
                degraded = http_ok(sample, source) is False
                result = safety.step(snapshot, decision["proposal"],
                                     observed_at=ts(sample["timestamp"]) if sample else None,
                                     source_degraded=verdict.ok and degraded,
                                     destination_responsive=http_ok(dest, OTHER[source]) is True)
                if not verdict.ok:
                    stage, reason = "confidence", verdict.reason.split(":")[0]
                elif result.state == "verifying":
                    stage, reason = "action", f"away_from:{source}"
                    action = {"at_unix": t, "away_from": source, "delay_s": round(t - slot["request_unix"]),
                              "route": f"{weight}->{route.read_weight()}", "confidence": verdict.reason}
                else:
                    stage, reason = "safety_controller", f"{result.state}:{','.join(result.reasons)}"
            if in_window:
                stages[stage][reason] += 1
                if stage != "no_proposal" and first["campaign_detector_or_lab_proposal"] is None:
                    first["campaign_detector_or_lab_proposal"] = round(t - slot["request_unix"])
                if stage in ("confidence", "safety_controller", "action") and first["reviewer_pass"] is None:
                    first["reviewer_pass"] = round(t - slot["request_unix"])
                if stage in ("safety_controller", "action") and first["confident"] is None:
                    first["confident"] = round(t - slot["request_unix"])
            elif action is not None:
                action["before_fault"] = True
            t += TICK_SEC
    furthest = next((s for s in reversed(STAGES) if stages[s]), "no_proposal")
    blocker = None if action else {"stage": furthest, "top_reasons": stages[furthest].most_common(3)}
    return {"slot": slot["slot"], "region": slot["region"], "control_region": slot["control_region"],
            "loss_percent": slot["loss_percent"], "action": action, "blocked_at": blocker,
            "first_seen_delay_s": first, "shift_proposals_by_blamed_region": {k: dict(v) for k, v in proposals.items()},
            "impaired_region_votes_in_window": {k: dict(v) for k, v in votes.items()},
            "ticks_by_stage": {s: sum(c.values()) for s, c in stages.items()}}


def summarise(rows: list[dict], slots: list[dict]) -> dict:
    by_slot = {s["slot"]: s for s in slots}
    hits = [r for r in rows if r["action"] and r["action"]["away_from"] == r["region"]]
    false_actions = []
    for r in rows:
        a = r["action"]
        if a and a["away_from"] == r["control_region"]:
            s = by_slot[r["slot"]]
            if s["control_start_unix"] <= a["at_unix"] <= s["control_end_unix"]:
                false_actions.append(r["slot"])
    blocked = Counter(r["blocked_at"]["stage"] for r in rows if r["blocked_at"])
    return {"trials": len(rows), "would_fail_over_away_from_impaired_region": len(hits),
            "failover_delays_s": sorted(r["action"]["delay_s"] for r in hits),
            "false_actions_in_control_windows": len(false_actions), "false_action_slots": false_actions,
            "not_acted_blocked_at_stage": dict(blocked)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--campaign-dir", required=True, type=Path)
    parser.add_argument("--proposer", choices=sorted(PROPOSERS), action="append")
    parser.add_argument("--start-weight", type=int, default=50)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    inputs = Inputs(args.campaign_dir)
    slots = [s for s in inputs.slots if s["effect_confirmed"]]
    report = {"report_version": "adaptivenet.action_replay_campaign.v1",
              "excluded_unconfirmed_slots": [s["slot"] for s in inputs.slots if not s["effect_confirmed"]],
              "start_region_a_percent": args.start_weight, "tick_seconds": TICK_SEC,
              "rules": asdict(ConfidenceRules()), "runs": {}}
    for proposer in args.proposer or ["campaign", "lab"]:
        rows = [replay_slot(inputs, s, proposer, start_weight=args.start_weight) for s in slots]
        report["runs"][proposer] = {"summary": summarise(rows, slots), "slots": rows}
        print(proposer, json.dumps(report["runs"][proposer]["summary"]), flush=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
