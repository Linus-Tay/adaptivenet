"""Compare a pending local shift with a fresh read of the gateway route."""
from typing import Literal

from .journal import PendingAction

RouteOutcome = Literal["target_observed", "original_observed", "unexpected_route", "unknown"]


def reconcile_route(pending: PendingAction, *, observed_region_a_percent: int | None) -> RouteOutcome:
    """Classify route state. Pass None if an authoritative fresh read failed."""
    original = pending.before.get("region_a_percent")
    target = (pending.expected or {}).get("region_a_percent")
    if not all(
        type(weight) is int and 0 <= weight <= 100
        for weight in (original, target, observed_region_a_percent)
    ) or original == target:
        return "unknown"
    if observed_region_a_percent == target:
        return "target_observed"
    if observed_region_a_percent == original:
        return "original_observed"
    return "unexpected_route"
