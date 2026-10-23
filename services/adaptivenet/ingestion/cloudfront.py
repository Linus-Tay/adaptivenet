"""Read-only AWS/CloudFront CloudWatch metric collection, global (not regional).

Runs via the already-required AWS CLI / SSO profile. Does not use AWS write APIs.
All missing data stays missing; standard CloudFront metrics do not need enhanced monitoring.
"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
import json
import math
import os
import re
import subprocess
import time
from .models import Observation

DISTRIBUTION_RE = re.compile(r"^[A-Z0-9]{8,32}$")
# Metric API statistics follow AWS/CloudFront documentation. Percent is 0-100.
STANDARD = {
    "cloudfront_requests": ("Requests", "Sum", "requests"),
    "cloudfront_4xx_pct": ("4xxErrorRate", "Average", "%"),
    "cloudfront_5xx_pct": ("5xxErrorRate", "Average", "%"),
    "cloudfront_total_error_pct": ("TotalErrorRate", "Average", "%"),
    "cloudfront_bytes_downloaded": ("BytesDownloaded", "Sum", "bytes"),
    "cloudfront_bytes_uploaded": ("BytesUploaded", "Sum", "bytes"),
}
# CloudFront reporting can lag. Annotate freshness separately from regional AMP freshness.
FRESH_SECONDS = 900

def distribution_id(env_file=".env") -> str | None:
    value = None
    try:
        from .config import read_env_file
        value = read_env_file(env_file).get("CLOUDFRONT_DISTRIBUTION_ID")
    except (OSError, ValueError):
        pass
    value = os.environ.get("CLOUDFRONT_DISTRIBUTION_ID", value)
    if value is None or not value.strip():
        return None
    value = value.strip()
    if not DISTRIBUTION_RE.fullmatch(value):
        raise ValueError("invalid CLOUDFRONT_DISTRIBUTION_ID; expected a CloudFront distribution ID")
    return value


def metric_queries(distribution: str) -> list[dict]:
    if not DISTRIBUTION_RE.fullmatch(distribution):
        raise ValueError("invalid CloudFront distribution ID")
    return [{"Id": f"m{i}", "MetricStat": {
        "Metric": {"Namespace": "AWS/CloudFront", "MetricName": name,
                   "Dimensions": [{"Name":"DistributionId","Value":distribution},
                                  {"Name":"Region","Value":"Global"}]},
        "Period": 60, "Stat": stat}, "ReturnData": True}
        for i, (_, (name, stat, unit)) in enumerate(STANDARD.items())]


def _stamp(value) -> float:
    # AWS CLI returns an ISO8601 datetime; verify timezone and finite timestamp.
    if not isinstance(value, str):
        raise ValueError("CloudWatch timestamp is not a string")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("CloudWatch timestamp lacks timezone")
    ts = dt.timestamp()
    if not math.isfinite(ts):
        raise ValueError("CloudWatch timestamp not finite")
    return ts


def parse_metric_data(payload: dict, distribution: str, now: float) -> list[Observation]:
    if not isinstance(payload, dict) or not isinstance(payload.get("MetricDataResults"), list):
        raise ValueError("invalid CloudWatch metric-data response")
    rows=[]
    pairs=list(STANDARD.items())
    for result in payload["MetricDataResults"]:
        if not isinstance(result, dict):
            continue
        identifier=result.get("Id", "")
        if not isinstance(identifier,str) or not re.fullmatch(r"m[0-5]",identifier):
            continue
        metric, (_, _, unit)=pairs[int(identifier[1:])]
        times=result.get("Timestamps")
        values=result.get("Values")
        if not isinstance(times,list) or not isinstance(values,list) or len(times)!=len(values):
            continue
        valid=[]
        for stamp, value in zip(times,values):
            try: ts=_stamp(stamp)
            except (ValueError,OverflowError): continue
            if (type(value) not in (int,float) or not math.isfinite(value) or value < 0
                or (unit=="%" and value>100) or ts>now+60 or now-ts>FRESH_SECONDS):
                continue
            valid.append((ts,float(value)))
        if not valid:
            continue
        # Most recent real CloudWatch point, do not synthesize values or zero-fill.
        ts, value=max(valid, key=lambda row:row[0])
        rows.append(Observation("global","aws_cloudwatch",metric,value,unit,ts,now,
                                source_id=distribution))
    return rows


def collect_cloudfront(distribution: str, profile: str, now: float | None=None, timeout=35):
    now=time.time() if now is None else now
    end=datetime.fromtimestamp(now,timezone.utc)
    start=end-timedelta(minutes=20)
    args=["aws","cloudwatch","get-metric-data", "--region","us-east-1",
          "--profile",profile,"--metric-data-queries",json.dumps(metric_queries(distribution)),
          "--start-time",start.isoformat(),"--end-time",end.isoformat(),
          "--scan-by","TimestampDescending","--output","json","--no-cli-pager"]
    result=subprocess.run(args, capture_output=True,text=True,timeout=timeout,check=False,
                          env={**os.environ,"AWS_PAGER":""})
    if result.returncode:
        # Never echo AWS CLI stderr, which could contain account or session details.
        raise RuntimeError(f"cloudfront_cloudwatch_query_failed_exit_{result.returncode}")
    try: body=json.loads(result.stdout)
    except (ValueError,TypeError) as exc:
        raise ValueError("cloudfront_cloudwatch_invalid_json") from exc
    return parse_metric_data(body,distribution,now)
