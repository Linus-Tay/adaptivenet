"""Offline, timestamp-safe builder using REAL archived snapshots and HTTP events.

No labels inferred from telemetry, fault-button clicks, or absence of events.
Existing snapshots are immutable. Human-reviewed manifest supplies ground truth.
"""
from __future__ import annotations
import copy
import json
from pathlib import Path
from typing import Any

from adaptivenet.ingestion.handoff import build_model_input
from adaptivenet.ingestion.fast_signals import build_fast_signals
from .feature_views import GROUPS, AWS_COLUMNS, RIPE_COLUMNS, REGIONS, project
from .sensor_readiness import external_sensor_ready
from .verified_labels import unix, validate_manifest


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        raise FileNotFoundError(path)
    rows = []
    with path.open(encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, 1):
            if line.strip():
                obj = json.loads(line)
                if not isinstance(obj, dict):
                    raise ValueError(f"{path}:{line_no} must be JSON object")
                rows.append(obj)
    return rows


def _asof_snapshot(snapshot: dict) -> tuple[dict, list[str]]:
    """Reconstruct at immutable collection timestamp, without future data.

    Snapshot generation was already an as-of operation in the upstream code.
    Raw observation `collected_at` is NOT retained per metric in snapshots;
    provenance therefore cannot independently prove all receipt timestamps.
    """
    at = unix(snapshot.get("computed_at_unix"))
    version = snapshot.get("contract_version")
    if version == "adaptivenet.features.v1":
        slow = build_model_input(snapshot, mode="combined", now=at)
    elif version == "adaptivenet.model_input.v1":
        slow = copy.deepcopy(snapshot)
        if slow.get("computed_at_unix") != at:
            raise ValueError("inconsistent snapshot timestamp")
    elif version is None and snapshot.get("feature_columns") and snapshot.get("features"):
        # `scripts/export_ml_dataset.py` drops the contract_version, but keeps
        # original values/provenance and source_ready_at_collection.
        slow = {
            "contract_version": "adaptivenet.model_input.v1",
            "snapshot_id": snapshot.get("snapshot_id"),
            "computed_at_unix": at,
            "ready_for_inference": bool(snapshot.get("source_ready_at_collection")),
            "feature_columns": snapshot["feature_columns"],
            "features": copy.deepcopy(snapshot["features"]),
            "missing": copy.deepcopy(snapshot.get("missing", {})),
            "provenance": copy.deepcopy(snapshot.get("provenance", {})),
            "quality": copy.deepcopy(snapshot.get("quality", {})),
            "actions_enabled": False,
        }
    else:
        raise ValueError(f"unsupported archived feature snapshot: {version}")
    if not isinstance(slow.get("snapshot_id"), str) or not slow["snapshot_id"].startswith("features-"):
        raise ValueError("invalid snapshot ID")
    notes = ["snapshot_attests_source_collection_before_its_timestamp; per_metric_receipt_times_not_exported"]
    slow["ready_for_inference"] = False  # no historic record is approved as LIVE input
    slow["actions_enabled"] = False
    # Never accept a source timestamp after the decision cutoff. Original AMP
    # snapshots may permit +30s clock skew; mask those columns, do not fabricate.
    for region in REGIONS:
        row = slow.get("features", {}).get(region)
        provenance = slow.get("provenance", {}).get(region, {})
        if not isinstance(row, dict) or not isinstance(provenance, dict):
            raise ValueError("missing region/features/provenance")
        for col in AWS_COLUMNS:
            entry = provenance.get(col)
            if isinstance(entry, dict) and entry.get("observed_at") is not None:
                if unix(entry["observed_at"]) > at:
                    row[col] = None
                    # EWMA and robust-z can include the same future-dated AMP
                    # observation. Never retain derived values after masking
                    # their direct source column.
                    dependents = {"cpu_busy_pct": ("cpu_busy_ewma_pct",),
                                  "http_p95_ms": ("http_p95_ewma_ms", "http_p95_robust_z")}
                    for derived in dependents.get(col, ()):
                        row[derived] = None
                    notes.append(f"masked_future_source_time:{region}.{col}")
        atlas = provenance.get("ripe_atlas")
        if isinstance(atlas, dict) and atlas.get("latest_observed_at") is not None:
            if unix(atlas["latest_observed_at"]) > at:
                for col in RIPE_COLUMNS:
                    row[col] = None
                notes.append(f"masked_future_ripe_source_time:{region}")
    return slow, notes


def make_dataset(snapshots: list[dict], witnesses: list[dict], manifest: dict) -> dict:
    episodes = validate_manifest(manifest)
    clean_witness = []
    for event in witnesses:
        if not isinstance(event, dict) or event.get("scope") not in ("region-a", "region-b", "cloudfront"):
            continue
        try:
            stamp = unix(event.get("timestamp"))
        except ValueError:
            continue
        # Witness probe() records completion timestamp, not its start. A future
        # completion must never enter an earlier feature window.
        clean_witness.append((stamp, event))
    clean_witness.sort(key=lambda e: e[0])
    seen = set()
    result = []
    skipped_unlabeled = 0
    for snap in sorted(snapshots, key=lambda r: (unix(r.get("computed_at_unix")), str(r.get("snapshot_id")))):
        slow, notes = _asof_snapshot(snap)
        stamp, ident = unix(slow["computed_at_unix"]), slow["snapshot_id"]
        if ident in seen:
            continue
        seen.add(ident)
        matches = [e for e in episodes if e["start_unix"] <= stamp < e["end_unix"]]
        if not matches:
            skipped_unlabeled += 1
            continue
        # At most one verified episode per region at a given moment; a time
        # point may have a region-a and a region-b episode concurrently.
        witness_asof = [e for ts, e in clean_witness if stamp - 900 <= ts <= stamp]
        fast = build_fast_signals(witness_asof, now=stamp)
        detection = {"contract_version": "adaptivenet.detection_input.v1", "slow": slow, "fast": fast}
        views = {}
        for group in GROUPS:
            view = project(detection if group == "aws_ripe_http" else slow, group)
            # Group-specific readiness: the upstream combined mode incorrectly
            # rejects AWS-only even when its internal source is valid.
            qualities = slow.get("quality", {})
            required = ("app_up", "memory_available_bytes", "memory_total_bytes")
            internal_ok = all((qualities.get(r) or {}).get("internal") == "fresh"
                              and all(slow["features"][r].get(c) is not None for c in required)
                              for r in REGIONS)
            external_ok = external_sensor_ready(qualities, slow["features"], slow.get("provenance", {}), stamp)
            view["ready_for_shadow_assessment"] = (internal_ok and (group == "aws_only" or external_ok)
                                                   and (group != "aws_ripe_http" or fast["ready_for_fast_assessment"]))
            view.pop("limitations", None)  # metadata is not a predictor feature
            views[group] = view
        for ep in matches:
            result.append({"snapshot_id": ident, "at_unix": stamp,
                           "episode_id": ep["episode_id"], "experiment_run_id": ep["experiment_run_id"], "label": ep["label"],
                           "region": ep["region"], "evidence_refs": ep["independent_evidence_refs"],
                           "reviewer": ep["reviewer"], "provenance": slow.get("provenance", {}),
                           "quality": slow.get("quality", {}), "notes": notes,
                           "views": copy.deepcopy(views)})
    return {"contract_version": "adaptivenet.offline_dataset.v1", "rows": result,
            "episodes": [{"episode_id": e["episode_id"], "experiment_run_id": e["experiment_run_id"],
                          "experiment_run_id_explicit": e["experiment_run_id_explicit"],
                          "label": e["label"],
                          "start_unix": e["start_unix"], "end_unix": e["end_unix"],
                          "region": e["region"]} for e in episodes],
            "skipped_unlabeled_snapshots": skipped_unlabeled,
            "raw_snapshot_count": len(snapshots), "http_witness_count": len(clean_witness),
            "limitations": (["no_unverified_training_labels", "http_one_network_vantage",
                             "upstream_snapshot_missing_per_metric_collected_at"] +
                            (["verified_episodes_missing_explicit_experiment_run_id"]
                             if any(not e["experiment_run_id_explicit"] for e in episodes)
                             else []))}


def build_from_files(snapshots: Path, witnesses: Path, labels: Path) -> dict:
    return make_dataset(read_jsonl(snapshots), read_jsonl(witnesses),
                        json.loads(labels.read_text(encoding="utf-8")))
