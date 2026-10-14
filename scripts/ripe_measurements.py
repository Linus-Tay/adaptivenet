#!/usr/bin/env python3
"""Inspect RIPE runs, or safely save replacement measurement IDs in local .env.

No RIPE measurements are created/stopped; reads public metadata/results only.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
from adaptivenet.ingestion.config import region_configs
from adaptivenet.ingestion.sources import atlas_tls_context, parse_atlas_latest

URL = "https://atlas.ripe.net/api/v2/measurements"
FRESH_SECONDS = 2100


def read_json(url: str) -> object:
    req = Request(url, headers={"Accept": "application/json", "User-Agent": "AdaptiveNet/RIPE-status"})
    with urlopen(req, timeout=18, context=atlas_tls_context()) as response:
        return json.load(response)


def metadata(measurement: int) -> dict:
    result = read_json(f"{URL}/{measurement}/")
    if not isinstance(result, dict) or result.get("type") != "ping":
        raise ValueError(f"{measurement}: expected an RIPE Atlas ping measurement")
    return result


def state(info: dict) -> str:
    status = info.get("status") or {}
    return str(status.get("name") or status.get("id") or "unknown") if isinstance(status, dict) else str(status)


def inspect(region: str, measurement: int) -> dict:
    info = metadata(measurement)
    try:
        raw = read_json(f"{URL}/{measurement}/latest/?versions=1")
    except HTTPError as exc:
        if exc.code != 404:
            raise
        raw = []  # Scheduled measurement may have no results yet.
    from adaptivenet.ingestion.config import RegionConfig
    cfg = RegionConfig(region, "", "", measurement)
    events = parse_atlas_latest(raw, cfg, time.time())
    observed = max((e.observed_at for e in events), default=None)
    age = None if observed is None else max(0, round(time.time() - observed))
    return {
        "region": region,
        "id": measurement,
        "status": state(info),
        "target": info.get("target"),
        "last_result_age_seconds": age,
        "fresh": age is not None and age <= FRESH_SECONDS,
        "distinct_probes": len({e.source_id.split("/")[-1] for e in events}),
    }


def save_ids(env_path: Path, a: int, b: int) -> Path | None:
    """Replace only ATLAS_MEASUREMENTS; preserve every other env setting."""
    if a <= 0 or b <= 0 or a == b:
        raise ValueError("two distinct positive measurement IDs required")
    content = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    line = f"ATLAS_MEASUREMENTS=region-a={a},region-b={b}"
    lines = [row for row in content.splitlines() if not re.match(r"^\s*ATLAS_MEASUREMENTS\s*=", row)]
    lines.append(line)
    updated = "\n".join(lines) + "\n"
    if content == updated:
        return None
    env_path.parent.mkdir(parents=True, exist_ok=True)
    if env_path.exists():
        backup_dir = ROOT / "data" / "config-backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"env-before-ripe-{time.time_ns()}.bak"
        shutil.copy2(env_path, backup)
        try: os.chmod(backup, 0o600)
        except OSError: pass
    fd, temp_name = tempfile.mkstemp(prefix=".env-tmp-", dir=str(env_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(updated)
        if env_path.exists():
            try: os.chmod(temp_name, env_path.stat().st_mode & 0o777)
            except OSError: pass
        else:
            try: os.chmod(temp_name, 0o600)
            except OSError: pass
        os.replace(temp_name, env_path)
    finally:
        if os.path.exists(temp_name): os.unlink(temp_name)
    return env_path


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    look = sub.add_parser("status", help="Read-only RIPE status for configured measurement IDs")
    look.add_argument("--env-file", default=str(ROOT / ".env"))
    set_ = sub.add_parser("set", help="Validate cloned IDs and update local .env only")
    set_.add_argument("--region-a", type=int, required=True)
    set_.add_argument("--region-b", type=int, required=True)
    set_.add_argument("--env-file", default=str(ROOT / ".env"))
    set_.add_argument("--offline", action="store_true", help="Skip public RIPE validation when offline")
    args = p.parse_args(argv)
    try:
        if args.command == "status":
            config = region_configs(args.env_file)
            rows = [inspect(c.name, c.measurement) for c in config]
        else:
            if args.region_a <= 0 or args.region_b <= 0 or args.region_a == args.region_b:
                p.error("use two distinct positive measurement IDs")
            rows = []
            if not args.offline:
                rows = [inspect("region-a", args.region_a), inspect("region-b", args.region_b)]
                if rows[0]["target"] == rows[1]["target"]:
                    p.error("both IDs target the same host; check region A and B")
                for row in rows:
                    if row["status"].lower() in ("stopped", "forced to stop", "failed", "archived"):
                        p.error(f"measurement {row['id']} is {row['status']}; use a running/scheduled clone")
            save_ids(Path(args.env_file), args.region_a, args.region_b)
            print("Saved new measurement IDs to local .env; no RIPE credits spent by this command.")
            if os.getenv("ATLAS_MEASUREMENTS"):
                print("WARNING: unset ATLAS_MEASUREMENTS in your shell; it overrides .env.")
        for row in rows:
            print(json.dumps(row, sort_keys=True))
        if args.command == "set":
            print("Restart ingestion watch, then run a new once cycle and check freshness.")
    except (OSError, ValueError, KeyError, TimeoutError, URLError) as exc:
        print(f"Unable to complete: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
