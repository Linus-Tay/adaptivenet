#!/usr/bin/env python3
"""Stop the RIPE Atlas measurements named in ATLAS_MEASUREMENTS (.env), to stop spending credits.

  python scripts/ripe_stop_measurements.py            # show what would be stopped
  python scripts/ripe_stop_measurements.py --execute  # stop them

Reads ATLAS_API_KEY from the environment or .env; the key is never printed.
"""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from adaptivenet.ingestion.config import read_env_file  # noqa: E402
from ripe_campaign_measurements import key_from  # noqa: E402  (env or .env, never printed)

API = "https://atlas.ripe.net/api/v2/measurements/{}/"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--env-file", type=Path, default=Path(".env"))
    ap.add_argument("--execute", action="store_true")
    a = ap.parse_args(argv)
    env = read_env_file(a.env_file)
    raw = os.getenv("ATLAS_MEASUREMENTS") or env.get("ATLAS_MEASUREMENTS", "")
    ids = [part.split("=", 1)[1].strip() for part in raw.split(",") if "=" in part]
    if not ids:
        raise SystemExit("ATLAS_MEASUREMENTS is not set; nothing to stop")
    if not a.execute:
        print("would stop:", ", ".join(ids)); return 0
    key = key_from(a.env_file)
    failed = 0
    for msm in ids:
        req = urllib.request.Request(API.format(msm), method="DELETE", headers={"Authorization": "Key " + key})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                print(msm, "stopped", resp.status)
        except urllib.error.HTTPError as exc:
            failed += 1
            print(msm, "not stopped: HTTP", exc.code)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
