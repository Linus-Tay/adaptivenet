"""Immutable normalized observations; no secrets, raw IPs or customer data."""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from hashlib import sha256
import json
import math

REGIONS = ("region-a", "region-b")
OBSERVATION_REGIONS = REGIONS + ("global",)

@dataclass(frozen=True)
class Observation:
    region: str
    source: str                 # "aws_amp" | "ripe_atlas"
    metric: str                 # fixed, documented metric vocabulary
    value: float
    unit: str
    observed_at: float          # source timestamp, UTC UNIX seconds
    collected_at: float         # local UTC UNIX seconds
    source_id: str = ""         # stable probe identifier, never customer PII
    labels: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if self.region not in OBSERVATION_REGIONS:
            raise ValueError("unrecognized region")
        if self.source not in ("aws_amp", "ripe_atlas", "aws_cloudwatch"):
            raise ValueError("unrecognized source")
        if (self.region == "global") != (self.source == "aws_cloudwatch"):
            raise ValueError("CloudFront CloudWatch metrics belong to global only")
        if not self.metric or not self.unit or not math.isfinite(self.value):
            raise ValueError("missing or non-finite metric")
        if not math.isfinite(self.observed_at) or self.observed_at <= 0:
            raise ValueError("invalid observed_at")
        if not math.isfinite(self.collected_at) or self.collected_at <= 0:
            raise ValueError("invalid collected_at")
        if self.observed_at - self.collected_at > 300:
            raise ValueError("source observation is too far in the future")
        if len(self.labels) > 8 or any(len(str(k)) > 64 or len(str(v)) > 128 for k,v in self.labels.items()):
            raise ValueError("labels must be low-cardinality")

    @property
    def event_id(self) -> str:
        # Neither poll time nor current local time participates: repeated upstream
        # results produce the same identity and are idempotent in SQLite.
        key = ["v1", self.region, self.source, self.metric, self.source_id,
               round(self.observed_at, 3), self.value, self.unit]
        return sha256(json.dumps(key, separators=(",", ":")).encode()).hexdigest()

    def record(self) -> dict:
        return {"event_id": self.event_id, **asdict(self)}
