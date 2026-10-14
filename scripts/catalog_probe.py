#!/usr/bin/env python3
"""Explicit, bounded, read-only HTTP GET probe to initialize real /api/catalog counters.

Does not write fake Prometheus metrics, change infrastructure or launch background jobs.
"""
import argparse
import json
import time
from urllib.request import urlopen

TARGETS = {"region-a": "http://54.179.156.53/api/catalog", "region-b": "http://100.58.157.137/api/catalog"}

def probe(region: str, url: str) -> tuple[int, str]:
    with urlopen(url, timeout=6) as response:
        if response.status != 200:
            raise ValueError(f"HTTP {response.status}")
        body = response.read(10000)
        result = json.loads(body)
        if result.get("region") != region:
            raise ValueError("unexpected region in response")
        return response.status, str(result["region"])

def main(argv=None):
    parser=argparse.ArgumentParser(description="Bounded catalog HTTP GET probes (no AWS mutations)")
    parser.add_argument("--run", action="store_true", help="Send actual GET requests to the two live EC2 demo endpoints")
    parser.add_argument("--count", type=int, default=3, help="GET requests per region, 1 to 10 (default 3)")
    args=parser.parse_args(argv)
    if not 1 <= args.count <= 10:
        parser.error("--count must be from 1 to 10")
    print(f"Plan: {args.count} GET /api/catalog requests to each deployed region (2*{args.count} total)")
    if not args.run:
        print("DRY RUN. Pass --run to execute bounded GET probes.")
        return 0
    failures=[]
    for region,url in TARGETS.items():
        for i in range(args.count):
            try:
                status,actual=probe(region,url)
                print(f"{region}: GET {i+1}/{args.count} HTTP {status} region={actual}")
            except Exception as e:
                failures.append((region, type(e).__name__))
                print(f"{region}: request {i+1} failed ({type(e).__name__})")
            time.sleep(0.25)
    if failures:
        print("Some requests failed. Check live IPs and /api/catalog public reachability; no AWS configuration was changed.")
        return 1
    print("HTTP probes completed. Allow at least 30-45 seconds for 15-second Prometheus scrapes and remote write before querying HTTP rates.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
