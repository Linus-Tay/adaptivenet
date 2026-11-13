"""Continuously archive existing SQLite and bounded HTTP journal (read-only).

A long-running supervised process reduces, but cannot eliminate, journal-rotation
loss. Source mutation and AWS calls are strictly prohibited.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from .archive import archive


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, default=Path("data/adaptivenet-ingestion.db"))
    p.add_argument("--witness", type=Path, default=Path("data/probe-events.jsonl"))
    p.add_argument("--out", type=Path, default=Path("data/ml-archive"))
    p.add_argument("--interval", type=float, default=10)
    p.add_argument("--once", action="store_true")
    args = p.parse_args(argv)
    if args.interval < 1 or args.interval > 300:
        p.error("interval must be 1 to 300 seconds")
    if args.db.resolve() == args.witness.resolve() or args.out.resolve() in (args.db.resolve(), args.witness.resolve()):
        p.error("sources must differ from archive output")
    args.out.mkdir(parents=True, exist_ok=True)
    # Prevent concurrent archivers overwriting one another's atomic results.
    lock_path = args.out / ".ml_archive_watch.lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        p.error(f"another archiver may be active ({lock_path}); inspect before removing stale lock")
    with os.fdopen(fd, "w", encoding="utf-8") as lock:
        lock.write(str(os.getpid()))
    print("Archive watcher reads sources without changing them. Stop with Ctrl+C.", flush=True)
    try:
        while True:
            now = datetime.now(timezone.utc).isoformat()
            try:
                snaps, events = archive(args.db, args.witness, args.out)
                result = {"at_utc": now, "new_snapshots": snaps, "new_witness_events": events,
                          "status": "ok", "warning": "archive_is_not_guaranteed_lossless_if_source_rotates_while_down"}
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                result = {"at_utc": now, "status": "error", "detail": type(exc).__name__,
                          "warning": "no_source_data_deleted; check archive coverage"}
                if args.once:
                    print(json.dumps(result), flush=True)
                    return 2
            print(json.dumps(result), flush=True)
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        lock_path.unlink(missing_ok=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
