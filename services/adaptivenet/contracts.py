from typing import Literal
from pydantic import BaseModel, Field

class Fault(BaseModel):
    region: Literal["region-a", "region-b"]
    latency_ms: int = Field(default=0, ge=0, le=5000)
    error_rate: float = Field(default=0, ge=0, le=1)
    loss_pct: float = Field(default=0, ge=0, le=100)
    duration_seconds: int = Field(default=30, ge=1, le=120)
