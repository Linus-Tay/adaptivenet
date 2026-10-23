#!/usr/bin/env python3
"""Real bounded-rate CloudFront + EC2 GET witnesses. Run with --run; Ctrl+C stops."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import time
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
from adaptivenet.ingestion.cloudfront import distribution_id
from adaptivenet.ingestion.sources import atlas_tls_context
from adaptivenet.ingestion.witness import append_probe

REGIONS = {
    "region-a": ("ap-southeast-1", "i-0039feaedbd2b3266"),
    "region-b": ("us-east-1", "i-0ea5125bcb2d201ec"),
}

def aws_text(args: list[str], profile: str) -> str:
    result = subprocess.run(["aws", *args, "--profile", profile, "--output", "text", "--no-cli-pager"],
                            text=True, capture_output=True, check=False, timeout=25)
    if result.returncode:
        raise RuntimeError("AWS read-only endpoint lookup failed; refresh SSO and check Describe permissions")
    return result.stdout.strip()

def resolve_targets(profile: str, env_file: Path, *, cloudfront_only: bool = False) -> dict[str, str]:
    did = distribution_id(str(env_file))
    if not did:
        raise ValueError("CloudFront ID missing from .env; run cloudfront_configure.py")
    domain = aws_text(["cloudfront", "get-distribution", "--region", "us-east-1", "--id", did,
                       "--query", "Distribution.DomainName"], profile)
    if not domain.endswith(".cloudfront.net") or any(c in domain for c in "/:@ \t\n"):
        raise ValueError("Unexpected CloudFront domain")
    targets = {"cloudfront": "https://" + domain + "/api/catalog"}
    if not cloudfront_only:
        for region, (aws_region, instance_id) in REGIONS.items():
            host = aws_text(["ec2", "describe-instances", "--region", aws_region,
                             "--instance-ids", instance_id,
                             "--query", "Reservations[0].Instances[0].PublicDnsName"], profile)
            if not host.startswith("ec2-") or not host.endswith((".compute.amazonaws.com", ".compute-1.amazonaws.com")):
                raise ValueError(f"{region} has no expected public EC2 DNS name")
            targets[region] = "http://" + host + "/api/catalog"
    return targets

def probe(scope: str, url: str, *, timeout: float = 8.0, context: ssl.SSLContext | None = None) -> dict:
    began = time.monotonic()
    code, origin, outcome = None, None, "network_error"
    try:
        req = Request(url, headers={"Cache-Control": "no-cache", "User-Agent": "AdaptiveNet-Witness/1.0"})
        with urlopen(req, timeout=timeout, context=context if url.startswith("https://") else None) as response:
            code = response.status
            try:
                data = json.loads(response.read(8192))
            except (ValueError, TypeError):
                data = {}
            origin = data.get("region") if isinstance(data, dict) else None
            outcome = "ok" if 200 <= code < 300 and origin in ("region-a", "region-b") else "invalid_response"
            if scope in REGIONS and origin != scope:
                outcome = "unexpected_region"
    except HTTPError as exc:
        code, outcome = exc.code, "http_error"
    except (URLError, TimeoutError, OSError):
        outcome = "network_error"
    return {"timestamp": datetime.now(timezone.utc).isoformat(), "scope": scope,
            "http_status": code, "latency_ms": round((time.monotonic()-began)*1000, 1),
            "origin": origin, "outcome": outcome, "kind": "http_witness"}

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="adaptivenet-admin")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--cycles", type=int, default=0, help="0 = until Ctrl+C")
    parser.add_argument("--cloudfront-only", action="store_true")
    parser.add_argument("--run", action="store_true", help="Send real HTTP GETs (bounded AWS traffic)")
    parser.add_argument("--out", default=str(ROOT / "data" / "probe-events.jsonl"))
    args = parser.parse_args(argv)
    if not (60 <= args.interval <= 3600 and 1 <= args.count <= 3 and 0 <= args.cycles <= 10000):
        parser.error("interval 60-3600; count 1-3; cycles 0-10000")
    targets = resolve_targets(args.profile, ROOT / ".env", cloudfront_only=args.cloudfront_only)
    for scope, url in targets.items():
        print(scope, url)
    print(f"Schedule: {args.count} GET per target every {args.interval} seconds.")
    if not args.run:
        print("DRY RUN. Add --run to generate approved real traffic.")
        return 0
    context = atlas_tls_context()
    try:
        import certifi
        context.load_verify_locations(cafile=certifi.where())
    except ImportError:
        pass
    n = 0
    try:
        while not args.cycles or n < args.cycles:
            started = time.monotonic()
            for scope, url in targets.items():
                for _ in range(args.count):
                    result = probe(scope, url, context=context)
                    append_probe(args.out, result)
                    print(json.dumps(result), flush=True)
            n += 1
            if args.cycles and n >= args.cycles:
                break
            time.sleep(max(0, args.interval - (time.monotonic()-started)))
    except KeyboardInterrupt:
        print("Witness stopped.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
