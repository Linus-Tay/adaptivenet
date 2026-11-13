#!/usr/bin/env python3
"""Create the campaign's 240 s RIPE ping measurements (same probes as before).

Reads ATLAS_API_KEY from the environment or a local .env file; the key is never
printed or written anywhere. Creates public measurements with an automatic stop
time. This spends RIPE credits and is run only with explicit operator approval.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import urllib.request

API = "https://atlas.ripe.net/api/v2/measurements/"
TARGETS = {"region-a": "54.179.156.53", "region-b": "100.58.157.137"}
PROBES = "13106,1008208,1010367,1015838"  # identical probe set to msm 235485629/235482125


def key_from(env_file: Path | None) -> str:
    key = os.getenv("ATLAS_API_KEY", "").strip()
    if not key and env_file and env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("ATLAS_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not key:
        raise SystemExit("ATLAS_API_KEY not set (add it to .env yourself; never paste it into chat)")
    return key


def payload(hours: float) -> dict:
    stop = datetime.now(timezone.utc) + timedelta(hours=hours)
    return {"definitions": [{"target": ip, "description": f"AdaptiveNet campaign {r} 240s ping",
                             "type": "ping", "af": 4, "interval": 240, "packets": 3, "is_public": True}
                            for r, ip in TARGETS.items()],
            "probes": [{"requested": 4, "type": "probes", "value": PROBES}],
            "is_oneoff": False, "stop_time": int(stop.timestamp())}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--hours", type=float, default=12)
    ap.add_argument("--env-file", type=Path, default=Path(".env"))
    ap.add_argument("--execute", action="store_true")
    a = ap.parse_args(argv)
    body = payload(a.hours)
    if not a.execute:
        print(json.dumps(body, indent=2)); return 0
    req = urllib.request.Request(API, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Key " + key_from(a.env_file)})
    with urllib.request.urlopen(req, timeout=60) as resp:
        ids = json.loads(resp.read().decode()).get("measurements", [])
    print("created (region-a, region-b):", ids)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
