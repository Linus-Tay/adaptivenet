"""Steady background user traffic on the user path (edge -> gateway -> origins)."""
from __future__ import annotations

import os
import time

import httpx


def main() -> None:
    url = os.environ["EDGE_URL"].rstrip("/") + "/http/cloudfront"
    interval = 1 / float(os.environ.get("USER_RPS", "2"))
    with httpx.Client(timeout=3, trust_env=False) as client:
        while True:
            started = time.monotonic()
            try:
                client.get(url)
            except httpx.HTTPError:
                pass
            time.sleep(max(0.0, interval - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
