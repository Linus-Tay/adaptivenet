"""Infrastructure references only, no secrets in source or recorded snapshots."""
from __future__ import annotations
from dataclasses import dataclass
import os
from pathlib import Path

DEFAULTS = {
    "region-a": {"aws_region": "ap-southeast-1", "workspace": "ws-12cfc829-fd81-440a-8c13-38cd28123fef", "measurement": 222734302},
    "region-b": {"aws_region": "us-east-1", "workspace": "ws-cc3d9d44-3a8e-49db-b275-0e8989ab47de", "measurement": 222734303},
}

@dataclass(frozen=True)
class RegionConfig:
    name: str
    aws_region: str
    workspace: str
    measurement: int


def read_env_file(path: str | Path | None) -> dict[str, str]:
    # Read only known non-secret infrastructure variables. Do not evaluate shell expressions.
    allowed = {"ATLAS_MEASUREMENTS", "AMP_SG_WORKSPACE_ID", "AMP_US_WORKSPACE_ID", "CLOUDFRONT_DISTRIBUTION_ID",
               "ADAPTIVENET_SQS_QUEUE_URL", "ADAPTIVENET_AWS_ACCOUNT_ID"}
    if not path or not Path(path).exists():
        return {}
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() in allowed:
            values[name.strip()] = value.strip().strip('"\'')
    return values


def region_configs(env_file: str | None = None) -> list[RegionConfig]:
    vars_ = {**read_env_file(env_file), **os.environ}
    measurements = {}
    raw = vars_.get("ATLAS_MEASUREMENTS", "")
    if raw:
        for part in raw.split(","):
            if part.count("=") != 1:
                raise ValueError("ATLAS_MEASUREMENTS must be region=integer pairs")
            name, mid = (v.strip() for v in part.split("=", 1))
            if name not in DEFAULTS or name in measurements or not mid.isdecimal() or int(mid) <= 0:
                raise ValueError("invalid or repeated RIPE Atlas measurement ID")
            measurements[name] = int(mid)
    out = []
    for name, cfg in DEFAULTS.items():
        env_name = "AMP_SG_WORKSPACE_ID" if name == "region-a" else "AMP_US_WORKSPACE_ID"
        workspace = vars_.get(env_name, cfg["workspace"])
        if not workspace.startswith("ws-") or not all(c.isalnum() or c == "-" for c in workspace):
            raise ValueError("invalid AWS Prometheus workspace ID")
        out.append(RegionConfig(name, cfg["aws_region"], workspace, measurements.get(name, cfg["measurement"])))
    return out
