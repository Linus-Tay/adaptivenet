#!/usr/bin/env python3
"""Export real stored, versioned feature snapshots for offline ML research.

No AWS calls and no fabricated labels. Display-only CloudFront last_known
values are explicitly removed by build_model_input; historical windows retain
original timestamps, missing masks and provenance.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
from adaptivenet.ingestion.store import Store
from adaptivenet.ingestion.handoff import build_model_input

def export_snapshots(db: str | Path, *, limit: int = 500, mode: str = "combined") -> list[dict]:
    if not 1 <= limit <= 500:
        raise ValueError("limit must be 1..500")
    snapshots = list(reversed(Store(db).recent_snapshots(limit)))
    results = []
    for snapshot in snapshots:
        stamp = snapshot["computed_at_unix"]
        model = build_model_input(snapshot, mode=mode, now=stamp)
        results.append({
            "snapshot_id": model["snapshot_id"],
            "computed_at_unix": stamp,
            "mode": model["mode"],
            "source_ready_at_collection": model["ready_for_inference"],
            "feature_columns": model["feature_columns"],
            "features": model["features"],
            "missing": model["missing"],
            "quality": model["quality"],
            "provenance": model["provenance"],
            "comparison": model["comparison"],
            "cloudfront_live_at_collection": model.get("cloudfront"),
            "fault_label": None,
            "label_source": None,
        })
    return results

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=str(ROOT/"data"/"adaptivenet-ingestion.db"))
    p.add_argument("--out", default=str(ROOT/"data"/"ml-snapshots.jsonl"))
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--mode", choices=("internal_only", "combined"), default="combined")
    args = p.parse_args(argv)
    rows = export_snapshots(args.db, limit=args.limit, mode=args.mode)
    out = Path(args.out)
    if not rows:
        print("No stored snapshots to export. Start ingestion watch first.", file=sys.stderr)
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, sort_keys=True, allow_nan=False)+"\n")
    print(f"Exported {len(rows)} timestamped real snapshots to {out}")
    print("fault_label is intentionally null: join verified experiment manifests, not SSM submissions.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
