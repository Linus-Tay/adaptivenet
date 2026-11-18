"""Confidence rules: when is the controller sure enough to act?

These rules sit in front of the existing safety reviewer and
``SafetyController``. They only ever make the controller *wait longer*; they
never approve an action on their own. ML / detector output is not read here:
every rule uses raw measurements, so ML stays shadow-only.

A **window** is one new feature snapshot (a new ``snapshot_id``; every 10-15 s
in the lab). Seeing the same snapshot twice counts once.

Three independent **signals** vote on each region in each window:

* ``aws``  AWS-internal metrics: app down, HTTP error ratio >= 20 % or p95 >= 800 ms
* ``ripe`` RIPE Atlas path: median probe loss >= 20 %, p50 RTT >= 400 ms or
  every probe unreachable (needs >= 3 probes and fresh data). With every probe
  lost there is no RTT, so ``RIPE_TOTAL_LOSS_RULE`` judges that case on loss
  alone. It is controller-only and leaves the frozen campaign detector alone.
* ``http`` the controller's own client request: failed, served by the wrong
  region, or missed the 500 ms deadline (``recovery.DEADLINE_SEC``)

A vote is ``bad``, ``good`` or ``unknown`` (stale or missing data). Unknown
never counts as good or bad.

Rules (defaults; all in ``ConfidenceRules``):

Failover (move 25 points away from a region)
  F1 at least 2 signals say the region is bad in the same window
  F2 F1 holds for 3 windows in a row (one window that fails F1 resets the count)
  F3 the other region has no bad vote and AWS says it is up
  F4 no flip-flop: at least 120 s since traffic last moved the other way
     (towards this region, e.g. a failback step)

Failback (move 25 points back to the primary, region-a)
  B1 the primary has no bad vote and at least 2 good votes, for 120 s in a row
     (any bad or under-reported window restarts the clock)
  B2 no flip-flop: at least 120 s since the last failover step

Scale up (raise the HPA minimum by one)
  S1 CPU >= 80 % with the app up, for 3 windows in a row
     (a stopped app is a failover case, not a scale case)
  S2 at least 300 s since the last scale change

Scale down (lower the HPA minimum by one, never below the baseline minimum)
  D1 CPU <= 30 %, error ratio < 5 % and the app up, for 300 s in a row
  D2 at least 300 s since the last scale change

After these pass, the existing gates still apply unchanged: the safety
reviewer, the 3 fresh controller probes, the 25-point step limit, the
journal, client recovery checks and rollback.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os

from .end_to_end import (
    MIN_RIPE_PROBES, NETWORK_LOSS_PCT, REGIONS, _number, internal_health, network_health,
)

PRIMARY = "region-a"
RULES_VERSION = "adaptivenet.confidence_rules.v1"
RIPE_TOTAL_LOSS_RULE = "ripe_loss_only_v1"  # added 2026-11-13, after the 20-trial RIPE campaign
SIGNALS = ("aws", "ripe", "http")


@dataclass(frozen=True)
class ConfidenceRules:
    min_bad_signals: int = 2            # F1
    failover_bad_windows: int = 3       # F2
    traffic_cooldown_sec: float = 120   # F4, B2
    failback_healthy_sec: float = 120   # B1
    min_good_signals: int = 2           # B1
    scale_up_cpu_pct: float = 80        # S1
    scale_up_windows: int = 3           # S1
    scale_down_cpu_pct: float = 30      # D1
    scale_down_max_error_ratio: float = 0.05  # D1
    scale_down_quiet_sec: float = 300   # D1
    scale_cooldown_sec: float = 300     # S2, D2

    @classmethod
    def from_env(cls, env=os.environ) -> "ConfidenceRules":
        """Lab overrides, e.g. ``CONFIDENCE_FAILOVER_BAD_WINDOWS=3``."""
        values = {}
        for name, default in cls().__dict__.items():
            raw = env.get(f"CONFIDENCE_{name.upper()}")
            if raw is not None:
                values[name] = type(default)(raw)
        return cls(**values)


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str


def signal_votes(snapshot: dict, region: str, http_ok: bool | None = None) -> dict[str, str]:
    """One window's votes for one region: {"aws": ..., "ripe": ..., "http": ...}."""
    aws = {"degraded": "bad", "healthy": "good"}.get(internal_health(snapshot, region), "unknown")
    ripe = {"degraded": "bad", "healthy": "good"}.get(network_health(snapshot, region)[0], "unknown")
    if ripe == "unknown":
        ripe = _ripe_loss_only_vote(snapshot, region)
    http = "unknown" if http_ok is None else ("good" if http_ok else "bad")
    return {"aws": aws, "ripe": ripe, "http": http}


def _ripe_loss_only_vote(snapshot: dict, region: str) -> str:
    """RIPE_TOTAL_LOSS_RULE: judge ``loss_only`` RIPE data on loss alone."""
    bundle = (snapshot.get("regions") or {}).get(region) or {}
    if (bundle.get("quality") or {}).get("external") != "loss_only":
        return "unknown"
    features = bundle.get("features") or {}
    probes, full = features.get("ripe_probes_reporting"), features.get("ripe_probes_full_loss")
    if type(probes) is not int or probes < MIN_RIPE_PROBES:
        return "unknown"
    if type(full) is int and full == probes:
        return "bad"
    loss = _number(features.get("packet_loss_median_pct"))
    if loss is None:
        return "unknown"
    return "bad" if loss >= NETWORK_LOSS_PCT else "good"


def _features(snapshot: dict, region: str) -> dict:
    return ((snapshot.get("regions") or {}).get(region) or {}).get("features") or {}


def _named(votes: dict, value: str) -> str:
    return "+".join(s.upper() for s in SIGNALS if votes.get(s) == value) or "none"


@dataclass
class _RegionState:
    bad_windows: int = 0
    healthy_since: float | None = None
    cpu_high_windows: int = 0
    quiet_since: float | None = None
    votes: dict = field(default_factory=dict)


class ConfidenceTracker:
    """Feed one window at a time with ``observe``; ask ``failover`` etc. when deciding."""

    def __init__(self, rules: ConfidenceRules | None = None):
        self.rules = rules or ConfidenceRules()
        self.regions = {r: _RegionState() for r in REGIONS}
        self.last_snapshot_id: str | None = None
        # (time, region that gained traffic) of the last applied route change
        self.last_traffic_change: tuple[float, str] | None = None
        self.last_scale_change: float | None = None

    # -- windows ---------------------------------------------------------
    def observe(self, snapshot: dict, *, now: float, http_ok: dict[str, bool | None] | None = None) -> bool:
        """Count one window. Returns False (and changes nothing) for a repeated snapshot."""
        snapshot_id = snapshot.get("snapshot_id")
        if not isinstance(snapshot_id, str) or snapshot_id == self.last_snapshot_id:
            return False
        self.last_snapshot_id = snapshot_id
        http_ok = http_ok or {}
        rules = self.rules
        for region, state in self.regions.items():
            votes = signal_votes(snapshot, region, http_ok.get(region))
            state.votes = votes
            bad = sum(v == "bad" for v in votes.values())
            good = sum(v == "good" for v in votes.values())
            state.bad_windows = state.bad_windows + 1 if bad >= rules.min_bad_signals else 0
            if bad == 0 and good >= rules.min_good_signals:
                state.healthy_since = now if state.healthy_since is None else state.healthy_since
            else:
                state.healthy_since = None

            features = _features(snapshot, region)
            cpu = _number(features.get("cpu_busy_pct"))
            errors = _number(features.get("http_error_ratio"))
            up = features.get("app_up") == 1 and votes["aws"] != "unknown"
            high = up and cpu is not None and cpu >= rules.scale_up_cpu_pct
            state.cpu_high_windows = state.cpu_high_windows + 1 if high else 0
            quiet = (up and cpu is not None and cpu <= rules.scale_down_cpu_pct
                     and errors is not None and errors < rules.scale_down_max_error_ratio)
            if quiet:
                state.quiet_since = now if state.quiet_since is None else state.quiet_since
            else:
                state.quiet_since = None
        return True

    def record_traffic_change(self, now: float, *, towards: str) -> None:
        """An applied route step. Each further step needs fresh bad windows."""
        self.last_traffic_change = (now, towards)
        for state in self.regions.values():
            state.bad_windows = 0

    def restart_failback_clock(self) -> None:
        """A failback step was rolled back: the primary must earn trust again."""
        self.regions[PRIMARY].healthy_since = None

    def record_scale_change(self, now: float) -> None:
        self.last_scale_change = now
        for state in self.regions.values():
            state.cpu_high_windows, state.quiet_since = 0, None

    # -- verdicts --------------------------------------------------------
    def _cooldown(self, last: float | None, seconds: float, now: float, what: str) -> Verdict | None:
        if last is not None and now - last < seconds:
            return Verdict(False, f"cooldown_{what}:{int(now - last)}/{int(seconds)}s")
        return None

    def _reversal_cooldown(self, towards: str, now: float) -> Verdict | None:
        """Block a move towards ``towards`` soon after traffic moved away from it."""
        last = self.last_traffic_change
        if last is None or last[1] == towards:
            return None
        return self._cooldown(last[0], self.rules.traffic_cooldown_sec, now, "reversal")

    def failover(self, region: str, now: float) -> Verdict:
        rules = self.rules
        if region not in self.regions:
            return Verdict(False, "unknown_region")
        state = self.regions[region]
        other = self.regions[REGIONS[1] if region == REGIONS[0] else REGIONS[0]]
        bad = sum(v == "bad" for v in state.votes.values())
        if bad < rules.min_bad_signals:                                      # F1
            return Verdict(False, f"signals_disagree:{bad}/{rules.min_bad_signals}_bad({_named(state.votes, 'bad')})")
        if state.bad_windows < rules.failover_bad_windows:                    # F2
            return Verdict(False, f"bad_windows:{state.bad_windows}/{rules.failover_bad_windows}")
        if "bad" in other.votes.values() or other.votes.get("aws") != "good":  # F3
            return Verdict(False, "other_region_not_clean")
        destination = REGIONS[1] if region == REGIONS[0] else REGIONS[0]
        cooling = self._reversal_cooldown(destination, now)                   # F4
        if cooling:
            return cooling
        return Verdict(True, f"confident:{_named(state.votes, 'bad')}_bad_for_{state.bad_windows}_windows")

    def failback(self, now: float) -> Verdict:
        rules = self.rules
        state = self.regions[PRIMARY]
        if state.healthy_since is None:                                       # B1
            return Verdict(False, f"primary_not_clean({_named(state.votes, 'good')}_good)")
        healthy_for = now - state.healthy_since
        if healthy_for < rules.failback_healthy_sec:
            return Verdict(False, f"primary_healthy:{int(healthy_for)}/{int(rules.failback_healthy_sec)}s")
        cooling = self._reversal_cooldown(PRIMARY, now)                        # B2
        if cooling:
            return cooling
        return Verdict(True, f"primary_healthy_for_{int(healthy_for)}s")

    def scale_up(self, region: str, now: float) -> Verdict:
        rules = self.rules
        if region not in self.regions:
            return Verdict(False, "unknown_region")
        windows = self.regions[region].cpu_high_windows
        if windows < rules.scale_up_windows:                                  # S1
            return Verdict(False, f"cpu_high_windows:{windows}/{rules.scale_up_windows}")
        cooling = self._cooldown(self.last_scale_change, rules.scale_cooldown_sec, now, "scale")  # S2
        return cooling or Verdict(True, f"cpu_high_for_{windows}_windows")

    def scale_down(self, region: str, now: float, *, current_min: int, baseline_min: int) -> Verdict:
        rules = self.rules
        if region not in self.regions:
            return Verdict(False, "unknown_region")
        if current_min <= baseline_min:
            return Verdict(False, "already_at_baseline")
        since = self.regions[region].quiet_since
        quiet_for = 0 if since is None else now - since
        if quiet_for < rules.scale_down_quiet_sec:                            # D1
            return Verdict(False, f"quiet:{int(quiet_for)}/{int(rules.scale_down_quiet_sec)}s")
        cooling = self._cooldown(self.last_scale_change, rules.scale_cooldown_sec, now, "scale")  # D2
        return cooling or Verdict(True, f"quiet_for_{int(quiet_for)}s")

    def summary(self) -> dict:
        """Small per-region view for the decision log and dashboard."""
        return {region: {"votes": dict(s.votes), "bad_windows": s.bad_windows,
                         "healthy_since": s.healthy_since, "cpu_high_windows": s.cpu_high_windows,
                         "quiet_since": s.quiet_since}
                for region, s in self.regions.items()}
