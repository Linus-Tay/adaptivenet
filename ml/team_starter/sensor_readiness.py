"""Conservative ML-only freshness and missingness checks for external RIPE views.

Never fabricate missing measurements; preserve zero packet loss as valid.
The source-provenance time must be at or before the prediction cutoff.
"""
from __future__ import annotations

import math

from .feature_views import REGIONS
from .verified_labels import unix

RIPE_REQUIRED = (
    "network_rtt_p50_ms", "network_rtt_p95_ms", "packet_loss_pct",
    "ripe_probes_reporting",
)
MAX_RIPE_AGE_SECONDS = 2100
MIN_RIPE_PROBES = 3


def external_sensor_ready(quality: dict, features: dict, provenance: dict, cutoff: float) -> bool:
    """Require real, timely per-region RIPE evidence in combined model arms.

    A stale `quality.external == fresh` flag cannot override missing/invalid or
    future-dated source evidence. `0` loss remains a genuine observation.
    """
    try:
        if not isinstance(cutoff, (int, float)) or isinstance(cutoff, bool) or not math.isfinite(cutoff):
            return False
        for region in REGIONS:
            q = quality.get(region, {})
            f = features.get(region, {})
            p = provenance.get(region, {})
            if not isinstance(q, dict) or not isinstance(f, dict) or not isinstance(p, dict):
                return False
            if q.get("external") != "fresh":
                return False
            for col in RIPE_REQUIRED:
                value = f.get(col)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    return False
            count = f["ripe_probes_reporting"]
            loss = f["packet_loss_pct"]
            if count < MIN_RIPE_PROBES or not 0 <= loss <= 100:
                return False
            if f["network_rtt_p50_ms"] < 0 or f["network_rtt_p95_ms"] < 0:
                return False
            atlas = p.get("ripe_atlas", {})
            if not isinstance(atlas, dict):
                return False
            observed_at = unix(atlas.get("latest_observed_at"))
            if not 0 <= cutoff - observed_at <= MAX_RIPE_AGE_SECONDS:
                return False
    except (TypeError, ValueError, OverflowError):
        return False
    return True
