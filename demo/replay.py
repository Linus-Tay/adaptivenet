"""Replay recorded REAL telemetry through the demo ML, reviewer and decision view.

Sources (read-only):
* an archive folder from ``python -m ml.team_starter.archive`` holding
  ``feature_snapshots.jsonl`` and optionally ``http_witness.jsonl``; or
* an ingestion SQLite database (``feature_snapshots`` table) plus an optional
  ``probe-events.jsonl`` witness journal.

Recorded snapshots are written, in their original order and spacing (divided by
``--speed``), into a SEPARATE replay data folder. Live freshness gates compare
against the wall clock, so each replayed record is shifted by one constant
offset; the original timestamps are kept under ``replay`` in every snapshot and
witness event, and every snapshot is marked ``demo.mode = replay``. Values,
missing fields and quality flags are never changed. The controller runs in
``review`` mode during replay, so nothing is actuated.
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time

from adaptivenet.ingestion.store import Store


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _parse(value: str) -> float | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return parsed.timestamp() if parsed.tzinfo else None


def load_snapshots(source: Path) -> list[dict]:
    if source.is_dir():
        path = source / "feature_snapshots.jsonl"
        rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    else:
        con = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            rows = [json.loads(b) for (b,) in con.execute(
                "SELECT body_json FROM feature_snapshots ORDER BY computed_at, snapshot_id")]
        finally:
            con.close()
    rows = [r for r in rows if isinstance(r, dict) and isinstance(r.get("computed_at_unix"), (int, float))]
    return sorted(rows, key=lambda r: (r["computed_at_unix"], r.get("snapshot_id", "")))


def load_witness(path: Path | None) -> list[dict]:
    if path is None or not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and _parse(row.get("timestamp")) is not None:
            rows.append(row)
    return sorted(rows, key=lambda r: _parse(r["timestamp"]))


def shift_snapshot(snapshot: dict, offset: float) -> dict:
    """Shift time fields by one constant; keep originals; leave values untouched."""
    out = json.loads(json.dumps(snapshot))
    original = out["computed_at_unix"]
    out["computed_at_unix"] = original + offset
    out["computed_at"] = _iso(original + offset)
    for bundle in (out.get("regions") or {}).values():
        for item in (bundle.get("provenance") or {}).values():
            for key in ("observed_at", "latest_observed_at"):
                ts = _parse(item.get(key)) if isinstance(item, dict) else None
                if ts is not None:
                    item[key] = _iso(ts + offset)
    out["replay"] = {"original_computed_at_unix": original, "offset_seconds": round(offset, 3),
                     "values_modified": False}
    out["demo"] = {"mode": "replay", "sources_emulated": False}
    return out


def shift_event(event: dict, offset: float) -> dict:
    original = event["timestamp"]
    return {**event, "timestamp": _iso(_parse(original) + offset), "replay_original_timestamp": original}


def replay(snapshots: list[dict], witness: list[dict], out: Path, *, speed: float = 1.0,
           start: float | None = None, sleep=time.sleep, clock=time.time, loop: bool = False,
           max_gap: float | None = None, start_at: float | None = None) -> int:
    """Gaps longer than ``max_gap`` playback seconds (collector outages) are shortened
    to ``max_gap``; the view then shows the snapshot's real age jump via ``replay``."""
    if not snapshots:
        raise SystemExit("no recorded snapshots found")
    if speed <= 0:
        raise ValueError("speed must be positive")
    out.mkdir(parents=True, exist_ok=True)
    store = Store(out / "adaptivenet-ingestion.db")
    journal = out / "probe-events.jsonl"
    if start_at is not None:
        snapshots = [s for s in snapshots if s["computed_at_unix"] >= start_at]
        witness = [e for e in witness if _parse(e["timestamp"]) >= start_at - 900]
        if not snapshots:
            raise SystemExit("no recorded snapshots after --start-at")
    origin = clock() if start is None else start
    written = 0
    w = 0
    while True:
        due, previous = origin, None
        for snap in snapshots:
            if previous is not None:
                gap = (snap["computed_at_unix"] - previous) / speed
                due += min(gap, max_gap) if max_gap else gap
            previous = snap["computed_at_unix"]
            wait = due - clock()
            if wait > 0:
                sleep(wait)
            offset = clock() - snap["computed_at_unix"]
            # Witness events recorded up to this snapshot, in order, same offset.
            with journal.open("a", encoding="utf-8") as f:
                while w < len(witness) and _parse(witness[w]["timestamp"]) <= snap["computed_at_unix"]:
                    f.write(json.dumps(shift_event(witness[w], offset), allow_nan=False) + "\n")
                    w += 1
            store.store_snapshot(shift_snapshot(snap, offset))
            written += 1
        if not loop:
            return written
        origin, w = clock(), 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", type=Path, required=True, help="archive folder or ingestion .db")
    p.add_argument("--witness", type=Path, help="probe-events.jsonl (default: archive http_witness.jsonl)")
    p.add_argument("--out", type=Path, default=Path("/app/data"))
    p.add_argument("--speed", type=float, default=1.0)
    p.add_argument("--loop", action="store_true")
    p.add_argument("--max-gap", type=float, default=float(os.environ.get("REPLAY_MAX_GAP", "120")), help="cap idle gaps (playback seconds); 0 = keep")
    p.add_argument("--start-at", default=os.environ.get("REPLAY_START_AT") or None, help="ISO-8601 UTC time to start from, e.g. 2026-11-09T01:30:00Z")
    args = p.parse_args(argv)
    if args.source.resolve() == (args.out / "adaptivenet-ingestion.db").resolve():
        p.error("replay output must not overwrite its source")
    witness = args.witness or (args.source / "http_witness.jsonl" if args.source.is_dir() else None)
    snapshots = load_snapshots(args.source)
    print(f"loaded {len(snapshots)} recorded snapshots; replaying at {args.speed}x into {args.out}", flush=True)
    count = replay(snapshots, load_witness(witness), args.out, speed=args.speed, loop=args.loop,
                   max_gap=args.max_gap or None,
                   start_at=_parse(args.start_at) if args.start_at else None)
    print(f"replayed {count} snapshots", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
