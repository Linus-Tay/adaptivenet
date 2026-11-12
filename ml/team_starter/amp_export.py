"""READ-ONLY export of AWS Managed Prometheus history for the AWS-only arm.

Uses exactly the collector's PromQL (`services/adaptivenet/ingestion/sources.py`)
via SigV4-signed `query_range` calls with the operator's SSO profile. No writes,
no new metrics, nothing stored except numeric results under ignored `data/`.
A step with no sample stays absent (missing), never zero.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlencode
import urllib.request

from adaptivenet.ingestion.config import DEFAULTS
from adaptivenet.ingestion.sources import PROMQL

AMP_URL = "https://aps-workspaces.{region}.amazonaws.com/workspaces/{ws}/api/v1/query_range"


def _signed_post(url: str, body: str, aws_region: str, profile: str) -> dict:
    import boto3
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    creds = boto3.Session(profile_name=profile).get_credentials().get_frozen_credentials()
    req = AWSRequest(method="POST", url=url, data=body,
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    SigV4Auth(creds, "aps", aws_region).add_auth(req)
    r = urllib.request.Request(url, data=body.encode(), headers=dict(req.headers.items()), method="POST")
    with urllib.request.urlopen(r, timeout=60) as resp:
        return json.loads(resp.read().decode())


def export(region: str, start: float, end: float, step: int, profile: str) -> list[dict]:
    cfg = DEFAULTS[region]
    url = AMP_URL.format(region=cfg["aws_region"], ws=cfg["workspace"])
    by_ts: dict[float, dict] = {}
    for metric, (expr, _unit) in PROMQL.items():
        q = expr.replace("{name}", region)
        t = start
        while t < end:  # AMP caps points per query; chunk by 6 h
            e = min(end, t + 6 * 3600)
            payload = _signed_post(url, urlencode({"query": q, "start": t, "end": e, "step": step}),
                                   cfg["aws_region"], profile)
            if payload.get("status") != "success":
                raise RuntimeError(f"AMP query failed for {metric}")
            series = payload["data"]["result"]
            if len(series) > 1:
                raise RuntimeError(f"AMP returned multiple series for {metric}")
            for ts, val in (series[0]["values"] if series else []):
                by_ts.setdefault(float(ts), {"ts": float(ts), "region": region})[metric] = float(val)
            t = e
    rows = sorted(by_ts.values(), key=lambda r: r["ts"])
    for r in rows:  # same derived column as the collector
        if r.get("http_rps") and "http_5xx_rps" in r:
            r["http_error_ratio"] = r["http_5xx_rps"] / r["http_rps"]
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--region", choices=sorted(DEFAULTS), required=True)
    ap.add_argument("--start", type=float, required=True)
    ap.add_argument("--end", type=float, required=True)
    ap.add_argument("--step", type=int, default=30)
    ap.add_argument("--profile", default="adaptivenet-ml")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    rows = export(a.region, a.start, a.end, a.step, a.profile)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    print(f"AMP rows: {len(rows)} -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
