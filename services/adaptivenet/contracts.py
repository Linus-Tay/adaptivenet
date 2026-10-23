from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field

class TelemetryEvent(BaseModel):
    schema_version: Literal[1] = 1
    event_id: str
    tenant_id: str
    application_id: str
    region: str
    source: Literal["atlas", "prometheus", "cloudwatch", "synthetic"]
    observed_at: datetime
    received_at: datetime
    measurement_id: str | None = None
    probe_id: str | None = None
    metric: str
    value: float | None = None
    unit: str
    outcome: Literal["success", "timeout", "error", "missing"]

class Fault(BaseModel):
    region: Literal["region-a", "region-b"]
    # network: injected on the user path (gateway); in-region probes do not see it.
    # compute: injected inside the regional workload; every observer sees it.
    kind: Literal["network", "compute"] = "network"
    latency_ms: int = Field(default=0, ge=0, le=5000)
    error_rate: float = Field(default=0, ge=0, le=1)
    duration_seconds: int = Field(default=30, ge=1, le=120)
