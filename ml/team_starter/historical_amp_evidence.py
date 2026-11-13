#!/usr/bin/env python3
"""Read-only historical AMP raw-sample exporter for AdaptiveNet ML evidence review.

No AWS writes, SSM actions, collector modifications or automatic labels.
The Prometheus instant query with a range selector returns original sample
stamps, unlike query_range which can repeat a last-known sample at evaluation
steps. Metadata is retained so reviewers can inspect sampling and gaps.

Run from the repository root after downloading this file to
ml/team_starter/historical_amp_evidence.py.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services"))
from adaptivenet.ingestion.config import region_configs

METRICS = {
    "app_up": 'adaptivenet_demo_up{region="{region}",project="adaptivenet"}',
    "scrape_up": 'up{project="adaptivenet"}',
    "http_requests": 'adaptivenet_http_requests_total{region="{region}",project="adaptivenet",route="/api/catalog"}',
}
SAFE_LABELS = ("region", "job", "instance", "project", "route", "status")


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("Provide explicit UTC timestamp ending in Z or +00:00")
    return parsed.astimezone(timezone.utc)


def extract_raw_samples(payload: dict, *, start_ts: float, end_ts: float) -> list[dict]:
    if payload.get("status") != "success" or payload.get("data", {}).get("resultType") != "matrix":
        raise ValueError("AMP did not return a successful matrix response")
    results = payload["data"].get("result", [])
    if not isinstance(results, list):
        raise ValueError("Invalid matrix result")
    series_out = []
    for series in results:
        labels = series.get("metric") or {}
        if not isinstance(labels, dict):
            continue
        points = []
        for pair in series.get("values", []):
            if not isinstance(pair, list) or len(pair) != 2:
                continue
            try:
                stamp = float(pair[0])
                val = float(pair[1])
            except (ValueError, TypeError):
                continue
            if not math.isfinite(stamp) or not start_ts <= stamp <= end_ts:
                continue
            points.append({"observed_at_utc": datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
                           "observed_at_unix": stamp,
                           "value": val if math.isfinite(val) else None})
        series_out.append({"labels": {key: str(labels[key]) for key in SAFE_LABELS if key in labels},
                           "samples": points})
    return series_out


def fetch_matrix(*, aws_region: str, workspace: str, profile: str,
                 expression: str, end_unix: float, timeout: int = 30) -> dict:
    endpoint = (f"https://aps-workspaces.{aws_region}.amazonaws.com/workspaces/"
                f"{workspace}/api/v1/query")
    command = ["awscurl", "--region", aws_region, "--service", "aps", "-X", "POST",
               endpoint, "--header", "Content-Type: application/x-www-form-urlencoded",
               "-d", urlencode({"query": expression, "time": str(end_unix)})]
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                            check=False, env={**os.environ, "AWS_PROFILE": profile, "AWS_PAGER": ""})
    if result.returncode:
        raise RuntimeError("Signed read-only AMP query failed; verify AWS SSO or AMP read permission")
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError("AMP query returned non-JSON data") from exc


def historical_expression(metric: str, region: str, duration: int) -> str:
    """Build a fixed read-only PromQL range selector without parsing PromQL braces.

    Prometheus selector braces are syntax, *not* Python format fields. Only the
    controlled region placeholder is replaced.
    """
    if metric not in METRICS or region not in ("region-a", "region-b"):
        raise ValueError("Unknown fixed metric or region")
    if not isinstance(duration, int) or isinstance(duration, bool) or not 60 <= duration <= 900:
        raise ValueError("Invalid historical range")
    return METRICS[metric].replace("{region}", region) + f"[{duration}s]"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--profile", default="adaptivenet-ml")
    p.add_argument("--region", choices=("region-a", "region-b"), default="region-a")
    p.add_argument("--start", required=True, help="UTC ISO 8601, e.g. 2026-10-23T07:34:00Z")
    p.add_argument("--end", required=True, help="UTC ISO 8601, e.g. 2026-10-23T07:42:00Z")
    p.add_argument("--env-file", default=".env")
    p.add_argument("--out", default="data/ml-archive/historical-amp-oct23-sg.json")
    args = p.parse_args(argv)
    try:
        start = parse_utc(args.start)
        end = parse_utc(args.end)
        duration = int((end - start).total_seconds())
        if not 60 <= duration <= 900:
            p.error("Historical window must be 60–900 seconds")
        cfg = next(cfg for cfg in region_configs(args.env_file) if cfg.name == args.region)
        results = {}
        for name in METRICS:
            expr = historical_expression(name, cfg.name, duration)
            response = fetch_matrix(aws_region=cfg.aws_region, workspace=cfg.workspace,
                                    profile=args.profile, expression=expr,
                                    end_unix=end.timestamp())
            results[name] = {"promql": expr,
                             "series": extract_raw_samples(response, start_ts=start.timestamp(),
                                                           end_ts=end.timestamp())}
        report = {"report_version": "adaptivenet.historical_amp_raw_samples.v1",
                  "evidence_status": "UNREVIEWED_NOT_GROUND_TRUTH",
                  "note": "Read-only historical AMP queries; metrics may be missing, stale, or insufficient to prove an external HTTP outage. No labels are assigned.",
                  "region": cfg.name, "aws_region": cfg.aws_region,
                  "workspace": cfg.workspace,
                  "query_start_utc": start.isoformat(), "query_end_utc": end.isoformat(),
                  "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                  "results": results}
        output = ROOT / args.out
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
        print("Wrote", output)
        for name, content in results.items():
            series = content["series"]
            points = [point for item in series for point in item["samples"]]
            count_zeros = sum(1 for point in points if point["value"] == 0)
            print(f"  {name}: series={len(series)} raw_samples={len(points)} zeros={count_zeros}")
        print("UNREVIEWED evidence only. No incident labels assigned.")
        return 0
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired, StopIteration) as exc:
        print(f"Could not retrieve evidence: {type(exc).__name__}: {str(exc)[:130]}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
