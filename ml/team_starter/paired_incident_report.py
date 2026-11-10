"""Paired, held-out native fault detection comparisons (no invented truth).

Consumes row-level predictions emitted ONLY by native verified-label training.
A missing arm, inconsistent held-out key, or no verified incidents is not evaluable.
This module never accesses AWS, mutates infrastructure, or creates labels.
"""
from __future__ import annotations
from collections import defaultdict
import statistics
from pathlib import Path
import json

from .feature_views import GROUPS

ALERT_LABELS = frozenset(("application_failure", "network_degradation", "partial_outage", "transient"))


def _identity(row: dict) -> tuple:
    return (row["episode_id"], row["snapshot_id"], float(row["at_unix"]), row["region"])


def compare_paired_incidents(dataset: dict, arm_files: dict[str, dict | None]) -> dict:
    """Return honest paired per-incident results on IDENTICAL held-out rows.

    The caller must pass native held-out predictions for all three feature arms.
    A fixture result proves only reporter logic, NEVER live detector performance.
    """
    if dataset.get("contract_version") != "adaptivenet.offline_dataset.v1":
        raise ValueError("offline dataset contract mismatch")
    report = {"report_version": "adaptivenet.paired_incident_evaluation.v1",
              "status": "not_evaluable", "forecasting_horizon_seconds": 0,
              "basis": "same_held_out_native_rows_only", "arm_order": list(GROUPS),
              "limitations": ["detection_not_forecasting", "independent_verified_incidents_required",
                              "HTTP_from_one_network_vantage_not_widespread_Internet_truth"]}
    missing = [name for name in GROUPS if not arm_files.get(name)]
    if missing:
        report["blockers"] = ["missing_trained_native_held_out_predictions:" + ",".join(missing)]
        return report
    indexed = {}
    for arm in GROUPS:
        payload = arm_files[arm]
        if payload.get("contract_version") != "adaptivenet.native_test_predictions.v1" or payload.get("group") != arm:
            raise ValueError("incompatible native prediction artifact for " + arm)
        if payload.get("test_only") is not True or payload.get("source") != "reviewed_native_episodes":
            raise ValueError("unverified / nontest predictions for " + arm)
        items = payload.get("rows", [])
        by_id = {_identity(r): r for r in items}
        if len(by_id) != len(items):
            raise ValueError("duplicate held-out predictions for " + arm)
        indexed[arm] = by_id
    keys = set(indexed[GROUPS[0]])
    if not keys:
        report["blockers"] = ["no_verified_held_out_rows"]
        return report
    for arm in GROUPS[1:]:
        if set(indexed[arm]) != keys:
            raise ValueError("arms do not cover identical held-out snapshot/episode/region/time keys")
    # Prediction IDs must come from the declared reviewed dataset, not merely
    # agree with each other. A stale or fixture prediction file is rejected.
    known_rows = {_identity(row) for row in dataset.get("rows", [])}
    if len(known_rows) != len(dataset.get("rows", [])):
        raise ValueError("duplicate keys in source verified dataset")
    if not keys.issubset(known_rows):
        raise ValueError("held-out predictions not contained in supplied verified dataset")
    for key in keys:
        first = indexed[GROUPS[0]][key]
        if any(indexed[arm][key].get("label") != first.get("label") or
               indexed[arm][key].get("experiment_run_id") != first.get("experiment_run_id")
               for arm in GROUPS[1:]):
            raise ValueError("truth/run disagreement between arms")
    episodes = {e["episode_id"]: e for e in dataset.get("episodes", [])}
    rows_by_episode = defaultdict(list)
    for key in sorted(keys):
        ep_id = key[0]
        if ep_id not in episodes:
            raise ValueError("held-out episode absent from verified dataset")
        expected = episodes[ep_id]
        exemplar = indexed[GROUPS[0]][key]
        if exemplar["label"] != expected["label"] or exemplar["experiment_run_id"] != expected["experiment_run_id"]:
            raise ValueError("held-out truth disagrees with reviewed manifest")
        rows_by_episode[ep_id].append(key)
    incident_ids = sorted(k for k in rows_by_episode if episodes[k]["label"] in ALERT_LABELS)
    if not incident_ids:
        report["blockers"] = ["no_verified_held_out_fault_incidents"]
        return report
    incident_rows = []
    for ep_id in incident_ids:
        e = episodes[ep_id]
        entries = sorted(rows_by_episode[ep_id], key=lambda k: (k[2], k[1]))
        row = {"episode_id": ep_id, "experiment_run_id": e["experiment_run_id"],
               "region": e["region"], "verified_onset_unix": e["start_unix"],
               "label": e["label"], "per_arm": {}}
        for arm in GROUPS:
            detections = [k[2] for k in entries if indexed[arm][k]["predicted_label"] in ALERT_LABELS]
            correct = [k[2] for k in entries if indexed[arm][k]["predicted_label"] == e["label"]]
            first = min(detections) if detections else None
            row["per_arm"][arm] = {
                "detected": first is not None,
                "first_alert_unix": first,
                "delay_seconds": max(0., first - float(e["start_unix"])) if first is not None else None,
                "correct_class_detected": bool(correct),
                "abstentions": sum(indexed[arm][k]["predicted_label"] is None for k in entries),
                "samples": len(entries),
            }
        incident_rows.append(row)
    summary = {}
    for arm in GROUPS:
        delays = [row["per_arm"][arm]["delay_seconds"] for row in incident_rows
                  if row["per_arm"][arm]["detected"]]
        false_alerts = 0
        healthy_sec = 0.
        for ep_id, epkeys in rows_by_episode.items():
            if episodes[ep_id]["label"] != "healthy":
                continue
            sorted_keys = sorted(epkeys, key=lambda k: (k[2], k[1]))
            prior_alert = False
            for key in sorted_keys:
                alert = indexed[arm][key]["predicted_label"] in ALERT_LABELS
                if alert and not prior_alert:
                    false_alerts += 1
                prior_alert = alert
            if len(sorted_keys) >= 2:
                healthy_sec += sorted_keys[-1][2] - sorted_keys[0][2]
        summary[arm] = {
            "incident_count": len(incident_rows), "detected_incidents": len(delays),
            "missed_incidents": len(incident_rows) - len(delays),
            "median_detection_delay_seconds_when_detected": statistics.median(delays) if delays else None,
            "correct_class_incidents": sum(row["per_arm"][arm]["correct_class_detected"] for row in incident_rows),
            "false_alert_transitions_in_healthy_episodes": false_alerts,
            "observed_healthy_hours": healthy_sec / 3600,
            "false_alerts_per_24h_observed_healthy": (false_alerts * 86400 / healthy_sec if healthy_sec > 0 else None),
            "test_abstentions": sum(indexed[arm][k]["predicted_label"] is None for k in keys),
            "test_samples": len(keys),
        }
    paired = {}
    for external in GROUPS[1:]:
        both = [(row["per_arm"]["aws_only"]["delay_seconds"], row["per_arm"][external]["delay_seconds"])
                for row in incident_rows
                if row["per_arm"]["aws_only"]["detected"] and row["per_arm"][external]["detected"]]
        paired["aws_only_vs_" + external] = {
            "both_detected": len(both),
            "median_aws_only_minus_external_delay_seconds_when_both_detected":
                statistics.median(a-b for a,b in both) if both else None,
            "external_only_detected": sum(not r["per_arm"]["aws_only"]["detected"] and r["per_arm"][external]["detected"] for r in incident_rows),
            "aws_only_only_detected": sum(r["per_arm"]["aws_only"]["detected"] and not r["per_arm"][external]["detected"] for r in incident_rows),
        }
    report.update({"status": "evaluated", "identical_held_out_rows": len(keys),
                   "verified_held_out_incidents": len(incident_rows),
                   "per_incident": incident_rows, "by_arm": summary, "paired": paired})
    return report


def load_paired_report(dataset_path: Path, predictions_dir: Path) -> dict:
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    arms = {}
    for arm in GROUPS:
        p = predictions_dir / f"test-predictions-{arm}.json"
        arms[arm] = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    return compare_paired_incidents(dataset, arms)
