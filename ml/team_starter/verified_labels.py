"""Strict, reviewer-verified incident manifests. No automatic labels from commands.

Labels describe measured *current* condition, not future predictions. An external
HTTP witness from one vantage cannot validate a geographically widespread outage.
"""
from __future__ import annotations
from datetime import datetime
import math
from typing import Any

VERSION = "adaptivenet.verified_episodes.v1"
LABELS = ("healthy", "application_failure", "network_degradation",
          "partial_outage", "transient", "recovery")
REGIONS = ("region-a", "region-b", "both")


def unix(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("invalid timestamp")
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or value <= 0:
            raise ValueError("invalid timestamp")
        return float(value)
    if not isinstance(value, str):
        raise ValueError("timestamp must have timezone or be Unix seconds")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid UTC timestamp") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("timestamp timezone required")
    return dt.timestamp()


def validate_manifest(manifest: dict) -> list[dict]:
    """Validate verified intervals; return normalized episodes with Unix times.

    Does not pretend that an evidence *reference* is proof by itself: independent
    evidence needs actual human review and must be retained separately.
    """
    if not isinstance(manifest, dict) or manifest.get("contract_version") != VERSION:
        raise ValueError("verified episode contract mismatch")
    episodes = manifest.get("episodes")
    if not isinstance(episodes, list):
        raise ValueError("episodes must be a list")
    result = []
    seen = set()
    for item in episodes:
        if not isinstance(item, dict):
            raise ValueError("episode must be an object")
        ident = item.get("episode_id")
        if not isinstance(ident, str) or not ident.strip() or ident in seen:
            raise ValueError("missing or duplicate episode_id")
        seen.add(ident)
        # Older v1 manifests without this field may still be parsed for
        # inspection, but MUST NOT be eligible for supervised training: phases
        # from the same experiment cannot otherwise be reliably grouped.
        explicit_run_id = "experiment_run_id" in item
        run_id = item.get("experiment_run_id", ident)
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError(f"episode {ident} has invalid experiment_run_id")
        label, region = item.get("label"), item.get("region")
        if label not in LABELS or region not in REGIONS:
            raise ValueError(f"invalid label or region for {ident}")
        if item.get("reviewed") is not True:
            raise ValueError(f"episode {ident} lacks independent review")
        reviewer = item.get("reviewer")
        refs = item.get("independent_evidence_refs")
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise ValueError(f"episode {ident} has no reviewer")
        if not isinstance(refs, list) or not refs or any(not isinstance(r, str) or not r.strip() for r in refs):
            raise ValueError(f"episode {ident} has no independent evidence refs")
        # Explicitly prevent single-vantage partial/widespread claims.
        if label == "partial_outage" and (type(item.get("independent_vantage_count")) is not int or
                                          item["independent_vantage_count"] < 2):
            raise ValueError(f"episode {ident} requires >=2 independent vantage points")
        start, end = unix(item.get("verified_start_utc")), unix(item.get("verified_end_utc"))
        if end <= start:
            raise ValueError(f"episode {ident} has invalid interval")
        reviewed_at = unix(item.get("reviewed_at_utc"))
        # Review is retrospective; feature construction must never use this timestamp.
        result.append({**item, "experiment_run_id": run_id,
                       "experiment_run_id_explicit": explicit_run_id,
                       "start_unix": start, "end_unix": end,
                       "reviewed_at_unix": reviewed_at})
    for i, a in enumerate(result):
        for b in result[i + 1:]:
            overlapping_region = a["region"] == b["region"] or "both" in (a["region"], b["region"])
            if overlapping_region and a["start_unix"] < b["end_unix"] and b["start_unix"] < a["end_unix"]:
                raise ValueError(f"overlapping verified episodes {a['episode_id']} / {b['episode_id']}")
    return sorted(result, key=lambda r: (r["start_unix"], r["episode_id"]))
