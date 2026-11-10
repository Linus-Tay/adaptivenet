"""Read-only incident reconciliation: raw AMP scrape stamps vs archived snapshots.

Creates an *unreviewed* revised label candidate and an audit, never verified
training labels, synthetic snapshots, AWS requests, or model artifacts.

Historical AMP samples were retrieved after the incident; without per-sample
receipt times they cannot be treated as features available at prediction time.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any

AMP_CONTRACT = "adaptivenet.historical_amp_raw_samples.v1"
LABEL_CONTRACT = "adaptivenet.verified_episodes.v1"
AUDIT_CONTRACT = "adaptivenet.incident_reconciliation.v1"


def _timestamp(value: Any) -> float:
    if not isinstance(value, str):
        raise ValueError("explicit UTC ISO-8601 timestamp required")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid UTC timestamp") from exc
    if dt.tzinfo is None or dt.utcoffset() is None or dt.utcoffset().total_seconds() != 0:
        raise ValueError("timestamp must use UTC offset +00:00 or Z")
    return dt.timestamp()


def _iso(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def _json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return data


def _jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8-sig") as stream:
        for line_no, line in enumerate(stream, 1):
            if not line.strip():
                continue
            obj = json.loads(line)
            if not isinstance(obj, dict):
                raise ValueError(f"{path.name}:{line_no} must be a JSON object")
            rows.append(obj)
    return rows


def _fingerprint(path: Path) -> dict:
    return {"filename": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _load_raw_amp(path: Path, region: str) -> tuple[dict, dict]:
    payload = _json(path)
    if payload.get("report_version") != AMP_CONTRACT or payload.get("region") != region:
        raise ValueError(f"raw AMP contract/region mismatch: {path.name}")
    retrieved = _timestamp(payload.get("retrieved_at_utc"))
    if payload.get("evidence_status") != "UNREVIEWED_NOT_GROUND_TRUTH":
        raise ValueError("unexpected raw AMP evidence status")
    raw = {}
    for metric in ("scrape_up", "app_up", "http_requests"):
        group = payload.get("results", {}).get(metric, {})
        series_list = group.get("series", [])
        if not isinstance(series_list, list):
            raise ValueError("invalid AMP series list")
        found = []
        for series in series_list:
            labels = series.get("labels") or {}
            if labels.get("job") != "adaptivenet_demo" or labels.get("project") != "adaptivenet":
                continue
            if metric == "scrape_up" and labels.get("region") not in (None, region):
                raise ValueError("scrape series contradicts report region")
            if metric != "scrape_up" and labels.get("region") != region:
                continue
            points = []
            seen = set()
            for point in series.get("samples", []):
                stamp = _timestamp(point.get("observed_at_utc"))
                if "observed_at_unix" in point and abs(float(point["observed_at_unix"]) - stamp) > 0.01:
                    raise ValueError("raw AMP original timestamps disagree")
                if stamp > retrieved:
                    raise ValueError("raw AMP sample after report retrieval")
                val = point.get("value")
                if val is not None and (isinstance(val, bool) or not isinstance(val, (float, int))
                                        or not math.isfinite(val)):
                    raise ValueError("invalid AMP numeric sample")
                if metric == "scrape_up" and val not in (0, 0.0, 1, 1.0):
                    raise ValueError("scrape_up must be 0 or 1")
                if stamp in seen:
                    raise ValueError("duplicate AMP sample timestamp")
                seen.add(stamp)
                points.append({"observed_at_utc": _iso(stamp), "observed_at_unix": stamp,
                               "value": val})
            points.sort(key=lambda x: x["observed_at_unix"])
            found.append(points)
        if len(found) != 1:
            raise ValueError(f"expected one {region} demo series for {metric}, got {len(found)}")
        raw[metric] = found[0]
    # Node scrape must be kept distinct from the application target.
    node = [s for s in payload["results"]["scrape_up"]["series"]
            if (s.get("labels") or {}).get("job") == "node"
            and (s.get("labels") or {}).get("project") == "adaptivenet"]
    if len(node) != 1:
        raise ValueError(f"expected exactly one node-exporter series in {region}")
    node_points = []
    for point in node[0].get("samples", []):
        node_points.append({"observed_at_unix": _timestamp(point["observed_at_utc"]),
                            "value": point.get("value")})
    raw["node_scrape_up"] = sorted(node_points, key=lambda x: x["observed_at_unix"])
    return raw, {"retrieved_at_utc": _iso(retrieved), "report_file": _fingerprint(path)}


def _http_failed(row: dict) -> bool:
    value = row.get("http_status")
    if value is None:
        return True
    try:
        return int(value) >= 500
    except (TypeError, ValueError):
        return False  # unknown nonnumeric statuses cannot establish an application outage


def reconcile(sg_amp: Path, va_amp: Path, snapshots: Path, witnesses: Path,
              candidate: Path, *, ssm_start_utc: str) -> tuple[dict, dict]:
    """Return (audit, revised unapproved candidate). Nothing is written here."""
    start = _timestamp(ssm_start_utc)
    sg, sg_meta = _load_raw_amp(sg_amp, "region-a")
    va, va_meta = _load_raw_amp(va_amp, "region-b")
    failures = [p for p in sg["scrape_up"] if p["value"] == 0 and p["observed_at_unix"] >= start]
    if len(failures) < 2:
        raise ValueError("fewer than two post-SSM demo scrape failures; cannot propose onset")
    first_down, last_down = failures[0]["observed_at_unix"], failures[-1]["observed_at_unix"]
    restored_scrape = next((p for p in sg["scrape_up"]
                            if p["observed_at_unix"] > last_down and p["value"] == 1), None)
    if not restored_scrape:
        raise ValueError("no subsequent demo scrape recovery; cannot bound episode")
    restore_stamp = restored_scrape["observed_at_unix"]
    if any(p["value"] == 0 and first_down <= p["observed_at_unix"] <= restore_stamp
           for p in sg["node_scrape_up"]):
        raise ValueError("node-exporter scrape failed too; demo-only attribution needs review")
    if any(p["value"] == 0 and first_down <= p["observed_at_unix"] <= restore_stamp
           for p in va["scrape_up"]):
        raise ValueError("Virginia demo also failed; cannot claim isolated Singapore event")

    http = sorted((( _timestamp(r.get("timestamp")), r) for r in _jsonl(witnesses)
                   if r.get("scope") in ("region-a", "region-b", "cloudfront")),
                  key=lambda item: item[0])
    # Use only failures genuinely observed near the SSM and the demo scrape failures.
    sg_failed = [(t, r) for t, r in http if r.get("scope") == "region-a"
                 and start - 1 <= t <= restore_stamp and _http_failed(r)]
    if len(sg_failed) < 2:
        raise ValueError("insufficient corroborating Singapore HTTP failures")
    last_http_failed = sg_failed[-1][0]
    recovered_http = next((t for t, r in http if r.get("scope") == "region-a"
                           and t > last_http_failed and r.get("http_status") == 200), None)
    if recovered_http is None:
        raise ValueError("no subsequent Singapore HTTP recovery")
    # Both sources must have completed the failures before claiming restored.
    if recovered_http <= last_down:
        raise ValueError("HTTP appears recovered before final AMP failed scrape; review required")
    # First independently observed successful HTTP response is an upper bound
    # for outage end; the later clean scrape also confirms recovery.
    end = recovered_http
    if not first_down < end:
        raise ValueError("invalid candidate interval")

    other_failures = [(t, r.get("scope")) for t, r in http
                      if start <= t <= end and r.get("scope") in ("region-b", "cloudfront")
                      and _http_failed(r)]
    if other_failures:
        raise ValueError("other region/cloudfront also experienced HTTP errors in interval")

    raw_snapshots = _jsonl(snapshots)
    covered = []
    for snapshot in raw_snapshots:
        stamp = _timestamp(snapshot.get("computed_at"))
        if not first_down <= stamp < end:
            continue
        if snapshot.get("contract_version") != "adaptivenet.features.v1":
            raise ValueError("unexpected archived snapshot contract")
        reg = snapshot.get("regions", {}).get("region-a", {})
        p = reg.get("provenance", {}).get("app_up", {})
        prov_stamp = _timestamp(p["observed_at"]) if p.get("observed_at") else None
        if prov_stamp is not None and prov_stamp > stamp:
            raise ValueError("snapshot app_up provenance later than snapshot cutoff")
        covered.append({"snapshot_id": snapshot.get("snapshot_id"),
                        "computed_at_utc": _iso(stamp),
                        "reported_app_up": reg.get("features", {}).get("app_up"),
                        "app_up_original_observed_at_utc": _iso(prov_stamp) if prov_stamp else None,
                        "app_up_age_at_snapshot_seconds": round(stamp - prov_stamp, 3) if prov_stamp else None,
                        "combined_ready_at_collection": snapshot.get("ready_for_model"),
                        "not_replaced_by_raw_amp": True})

    draft = _json(candidate)
    if draft.get("contract_version") != LABEL_CONTRACT or len(draft.get("episodes", [])) != 1:
        raise ValueError("expected one draft episode, matching contract")
    old = draft["episodes"][0]
    if old.get("reviewed") is not False or old.get("reviewer") is not None:
        raise ValueError("refusing to rewrite an already reviewed candidate")
    if old.get("label") != "application_failure" or old.get("region") != "region-a":
        raise ValueError("unexpected episode class/region")
    revised = copy.deepcopy(draft)
    updated = revised["episodes"][0]
    updated["verified_start_utc"] = _iso(first_down)
    updated["verified_end_utc"] = _iso(end)
    updated["reviewed"] = False
    updated["reviewer"] = None
    updated["reviewed_at_utc"] = None
    refs = list(updated.get("independent_evidence_refs") or [])
    for additional in ("nov09-region-a-amp.json", "nov09-region-b-amp.json",
                       "nov09-scrape-target-review.json"):
        if additional not in refs:
            refs.append(additional)
    updated["independent_evidence_refs"] = refs
    updated["notes"] = (
        "DRAFT: revised candidate onset uses first post-SSM failed Singapore "
        "adaptivenet_demo Prometheus scrape, not a fabricated feature snapshot; "
        "HTTP failure and recovery independently corroborate demo-service impact; "
        "Virginia and node exporter remained scraped. At least one native snapshot "
        "may have stale app_up=1 despite the demonstrated failure. Raw AMP samples "
        "were retrieved later and MUST NOT be backfilled as live-time model features. "
        "First HTTP failure slightly precedes SSM reported start; timestamps are "
        "observation boundaries, not exact physical onset. Not a network outage. "
        "Independently reviewed evidence and verifier identity still required."
    )
    audit = {
        "contract_version": AUDIT_CONTRACT,
        "experiment_run_id": updated["experiment_run_id"],
        "status": "UNREVIEWED_RETROSPECTIVE_EVIDENCE_ONLY",
        "ssm_reported_start_utc": _iso(start),
        "proposed_onset_source": "sg:up{job=adaptivenet_demo,project=adaptivenet}:first_failed_scrape_after_ssm",
        "proposed_start_utc": _iso(first_down),
        "proposed_end_utc": _iso(end),
        "proposed_end_source": "sg:first_http_200_after_last_observed_failure",
        "sg_demo_scrape_failed_count": len(failures),
        "sg_first_clean_demo_scrape_utc": _iso(restore_stamp),
        "sg_http_failure_count_in_nearby_window": len(sg_failed),
        "sg_first_http_recovery_utc": _iso(recovered_http),
        "va_demo_scrape_failed_count_during_fault": 0,
        "snapshots_in_revised_candidate_interval": covered,
        "snapshot_count_in_revised_candidate_interval": len(covered),
        "retrospective_amp_reports": [sg_meta, va_meta],
        "input_fingerprints": [_fingerprint(p) for p in (snapshots, witnesses, candidate)],
        "raw_amp_original_sample_observed_times_preserved": True,
        "raw_amp_receipt_times_at_original_cutoffs_known": False,
        "raw_amp_eligible_as_live_model_features": False,
        "original_feature_snapshots_immutable": True,
        "automatically_approved_training_labels": 0,
        "reviewed": False,
        "limitations": [
            "start_and_end_boundaries_are_sensor_observations_not_exact_physical_times",
            "no_per_metric_original_receipt_time_for_retrospective_amp_samples",
            "one_independent_http_witness_vantage_only",
            "one_physical_fault_run_cannot_support_independent_train_validation_test",
            "candidate_must_be_independently_reviewed_before_native_dataset_use",
        ],
    }
    return audit, revised


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--sg-amp", required=True, type=Path)
    p.add_argument("--va-amp", required=True, type=Path)
    p.add_argument("--snapshots", required=True, type=Path)
    p.add_argument("--witness", required=True, type=Path)
    p.add_argument("--draft", required=True, type=Path)
    p.add_argument("--ssm-start-utc", required=True)
    p.add_argument("--out-prefix", required=True, type=Path)
    args = p.parse_args(argv)
    try:
        audit, revised = reconcile(args.sg_amp, args.va_amp, args.snapshots,
                                   args.witness, args.draft, ssm_start_utc=args.ssm_start_utc)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        p.exit(2, f"Incident reconciliation refused: {type(exc).__name__}: {exc}\n")
    audit_path = Path(str(args.out_prefix) + "-audit.json")
    candidate_path = Path(str(args.out_prefix) + "-review-required.json")
    for path, obj in ((audit_path, audit), (candidate_path, revised)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    print("Raw demo scrape failures:", audit["sg_demo_scrape_failed_count"])
    print("Proposed reviewed-onset candidate:", audit["proposed_start_utc"])
    print("Proposed interval end:", audit["proposed_end_utc"])
    print("Native snapshot(s) in candidate interval:", audit["snapshot_count_in_revised_candidate_interval"])
    for row in audit["snapshots_in_revised_candidate_interval"]:
        print("  Snapshot", row["snapshot_id"], "app_up=", row["reported_app_up"],
              "source_observed_at=", row["app_up_original_observed_at_utc"])
    print("Audit:", audit_path)
    print("UNREVIEWED draft:", candidate_path)
    print("No AWS requests; no verified labels; no source snapshots modified; no model training.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
