"""Read-only historical feature availability audit for AdaptiveNet.

A completed snapshot is an as-of artifact; it is not evidence of the exact
receipt time of each individual source observation. This audit never invents
receipt timestamps, incident labels, or model performance.

Standalone: python -m ml.team_starter.availability_audit ...
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3

CONTRACT = "adaptivenet.availability_audit.v1"
SNAPSHOT_CONTRACT = "adaptivenet.features.v1"


def _unix(value):
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("timestamp must be finite")
        return number
    if not isinstance(value, str):
        raise ValueError("timestamp must be UTC ISO-8601 or Unix seconds")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timestamp lacks time zone")
    return dt.timestamp()


def _provenance_items(snapshot):
    regions = snapshot.get("regions", {})
    if not isinstance(regions, dict):
        raise ValueError("regions must be an object")
    for region, region_data in regions.items():
        if not isinstance(region_data, dict):
            continue
        for metric, entry in region_data.get("provenance", {}).items():
            if not isinstance(entry, dict):
                continue
            key = "latest_observed_at" if metric == "ripe_atlas" else "observed_at"
            if entry.get(key) is not None:
                yield f"{region}.{metric}", entry[key]
    cloudfront = snapshot.get("cloudfront", {})
    if isinstance(cloudfront, dict):
        for metric, entry in cloudfront.get("provenance", {}).items():
            if isinstance(entry, dict) and entry.get("observed_at") is not None:
                yield f"cloudfront.{metric}", entry["observed_at"]


def load_archive(path: Path):
    result = []
    with path.open(encoding="utf-8-sig") as stream:
        for line_no, line in enumerate(stream, 1):
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"archive line {line_no} is not an object")
                result.append(value)
    return result


def read_journal(db_path: Path):
    """Open a consistent SQLite read snapshot, without changing WAL or schema."""
    uri = db_path.resolve().as_uri() + "?mode=ro"
    # sqlite3.Connection.__exit__ handles transactions but DOES NOT close the
    # connection. On Windows that leaves the DB file locked during temporary
    # directory cleanup; closing() releases the handle on every exit path.
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.execute("BEGIN")
        snap_rows = conn.execute(
            "SELECT snapshot_id,computed_at FROM feature_snapshots").fetchall()
        run_rows = conn.execute(
            "SELECT collected_at,source_status,errors_json FROM ingestion_runs").fetchall()
        conn.rollback()  # end the consistent read transaction; never write
    snaps = {ident: float(at) for ident, at in snap_rows}
    run_times = [float(r[0]) for r in run_rows]
    statuses = Counter(str(r[1]) for r in run_rows)
    return snaps, run_times, statuses


def audit(archive, sqlite_snaps, run_times, statuses):
    counters = Counter()
    issues = []
    seen = set()
    eps = 0.001
    for index, snap in enumerate(archive, 1):
        counters["archive_snapshots"] += 1
        ident = snap.get("snapshot_id")
        if not isinstance(ident, str) or not ident.startswith("features-"):
            issues.append({"index": index, "type": "invalid_snapshot_id"})
            continue
        if ident in seen:
            issues.append({"index": index, "type": "duplicate_archived_snapshot", "snapshot_id": ident})
        seen.add(ident)
        if snap.get("contract_version") != SNAPSHOT_CONTRACT:
            issues.append({"index": index, "type": "unexpected_snapshot_contract", "snapshot_id": ident})
            continue
        try:
            cutoff = _unix(snap["computed_at_unix"])
        except (ValueError, TypeError, KeyError, OverflowError):
            issues.append({"index": index, "type": "invalid_evaluation_timestamp", "snapshot_id": ident})
            continue
        if ident in sqlite_snaps:
            counters["matched_current_sqlite_snapshots"] += 1
            if abs(cutoff - sqlite_snaps[ident]) > eps:
                issues.append({"index": index, "type": "snapshot_cutoff_mismatch", "snapshot_id": ident})
            if not any(abs(cutoff - t) <= eps for t in run_times):
                issues.append({"index": index, "type": "current_sqlite_run_unmatched", "snapshot_id": ident})
            else:
                counters["matched_current_ingestion_runs"] += 1
        else:
            # Archive may retain snapshots after live SQLite retention/pruning.
            # Absence in current SQLite is a coverage limitation, NOT fabrication.
            counters["not_in_current_sqlite"] += 1
        for metric, timestamp in _provenance_items(snap):
            counters["timestamped_source_provenance_entries"] += 1
            try:
                observed = _unix(timestamp)
            except (ValueError, TypeError, OverflowError):
                issues.append({"index": index, "type": "invalid_source_timestamp",
                               "snapshot_id": ident, "feature": metric})
                continue
            if observed > cutoff + eps:
                counters["future_dated_source_entries"] += 1
                issues.append({"index": index, "type": "future_source_timestamp",
                               "snapshot_id": ident, "feature": metric,
                               "ahead_seconds": round(observed - cutoff, 3)})
        for region_data in snap.get("regions", {}).values():
            if isinstance(region_data, dict):
                counters["null_feature_values"] += sum(
                    value is None for value in region_data.get("features", {}).values())
    blockers = []
    if issues:
        blockers.append("timestamp_or_integrity_issues_found")
    if counters["not_in_current_sqlite"]:
        blockers.append("some_archived_snapshots_not_in_current_sqlite_journal")
    blockers.append("exact_per_metric_receipt_timestamps_not_recorded")
    return {
        "report_version": CONTRACT,
        "status": "issues_found" if issues else "limited_availability_evidence",
        "counts": dict(sorted(counters.items())),
        "journal_status_counts": dict(sorted(statuses.items())),
        "issues": issues[:100],
        "total_issue_count": len(issues),
        "limitations": blockers,
        "strict_historical_raw_replay_certified": False,
        "original_snapshot_asof_source_date_check": not issues,
        "changes_to_aws_or_ingestion": False,
        "interpretation": (
            "Persisted snapshots originate after synchronous source-poll completion. "
            "This checks source dates and journal alignment, NOT per-metric "
            "network receipt time; do not infer exact availability or create labels."),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshots", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    snapshots = load_archive(args.snapshots)
    sqlite_snaps, run_times, statuses = read_journal(args.db)
    report = audit(snapshots, sqlite_snaps, run_times, statuses)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("Status:", report["status"])
    print("Counts:", report["counts"])
    print("Issue count:", report["total_issue_count"])
    print("Important: exact receipt times remain UNVERIFIED")
    return 2 if report["total_issue_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
