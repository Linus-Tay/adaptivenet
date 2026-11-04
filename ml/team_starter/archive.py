"""Manual, non-destructive snapshot + witness archival for ML experiments.

Run from the repo root BEFORE SQLite's seven-day retention and the HTTP witness
journal's 512-KiB rotation discard older samples. This is not a daemon.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile


def _jsonl(path: Path):
    if not path.exists():
        return
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def _atomic_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".archive-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def archive(db: Path, witness: Path, out: Path) -> tuple[int, int]:
    """Preserve all currently available snapshots and journal rows; never label them.

    One writer at a time. This is a periodic snapshot archive, not an assurance
    against samples rotated away *before* this function runs.
    """
    if not db.is_file():
        raise FileNotFoundError(f"SQLite source missing: {db}")
    out.mkdir(parents=True, exist_ok=True)
    snapshots_path, probes_path = out / "feature_snapshots.jsonl", out / "http_witness.jsonl"
    source = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        rows = source.execute("SELECT snapshot_id, body_json FROM feature_snapshots ORDER BY computed_at, snapshot_id").fetchall()
    finally:
        source.close()
    old_snapshots = {r["snapshot_id"]: r for r in _jsonl(snapshots_path)}
    count_old = len(old_snapshots)
    for ident, body in rows:
        obj = json.loads(body)
        if obj.get("snapshot_id") != ident:
            raise ValueError("SQLite snapshot ID mismatch")
        old_snapshots[ident] = obj
    snapshots = sorted(old_snapshots.values(), key=lambda r: (r.get("computed_at_unix", 0), r["snapshot_id"]))
    _atomic_jsonl(snapshots_path, snapshots)

    old_probes = {_digest(e): e for e in _jsonl(probes_path)}
    probes_before = len(old_probes)
    for row in _jsonl(witness):
        if not isinstance(row, dict) or not row.get("timestamp") or not row.get("scope"):
            continue
        old_probes[_digest(row)] = row
    probes = sorted(old_probes.values(), key=lambda r: (r["timestamp"], r["scope"]))
    _atomic_jsonl(probes_path, probes)
    return len(snapshots) - count_old, len(probes) - probes_before


def _digest(obj: dict) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, default=Path("data/adaptivenet-ingestion.db"))
    p.add_argument("--witness", type=Path, default=Path("data/probe-events.jsonl"))
    p.add_argument("--out", type=Path, default=Path("data/ml-archive"))
    args = p.parse_args(argv)
    if args.out.resolve() in (args.db.resolve(), args.witness.resolve()):
        p.error("output must not overwrite source")
    new_snapshots, new_probes = archive(args.db, args.witness, args.out)
    print(f"Archived {new_snapshots} new snapshots and {new_probes} new HTTP events to {args.out}")
    print("These records are UNLABELED. Do not train as if missing labels mean healthy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
