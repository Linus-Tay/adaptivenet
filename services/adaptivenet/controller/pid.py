"""SHADOW-ONLY PID weight controller for the region-a / region-b traffic split.

Pure arithmetic: no network, no gateway writes, no AWS. `step()` returns a
*proposal* (`target_region_a_percent`); nothing here applies it. See
docs/PID_WEIGHT_CONTROLLER.md for the design, tuning and where PID does not apply.

Signal
    pressure(region) = max(p95_ms / target_p95_ms, error_ratio / target_error_ratio)
    excess(region)   = min(excess_clip, max(0, pressure - 1 - deadband))   # 0 while on target
    error            = excess(region-b) - excess(region-a)   # > 0 means region-a deserves more
                       (0 when both regions are over target by a similar amount)

Sampling
    step() may be called on every fast tick. It only decides once per sample interval,
    on the MEDIAN pressure of the ticks seen since the last decision, so one slow probe
    cannot move traffic. The newest tick must itself be known for a decision to be made.

Form
    Velocity (incremental) PID. The integral lives in the weight itself, so clamping
    the weight clamps the integral, and every step is re-based on the weight that is
    ACTUALLY applied. A shadow controller whose output is never applied therefore
    cannot wind up, and an operator override or a blocked write is picked up bumplessly.

Unknown data
    null / stale / under-sampled observations are UNKNOWN, never zero. The controller
    then holds its last output, integrates nothing and forgets its derivative history.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import statistics

REGION_A, REGION_B = "region-a", "region-b"
PROPOSAL_VERSION = "adaptivenet.proposal.v1"
MODEL_ID = "pid_weight_shadow_v1"


@dataclass(frozen=True)
class PIDConfig:
    target_p95_ms: float = 400.0         # setpoint; engine.py calls a region degraded at 800
    target_error_ratio: float = 0.05     # setpoint; engine.py calls a region degraded at 0.20
    kp: float = 3.0                      # pp of weight per unit of error
    ki: float = 0.25                     # pp of weight per unit of error per second
    kd: float = 0.0                      # pp-seconds per unit of error; 0 = PI (p95 is too noisy for D)
    sample_interval_s: float = 15.0      # slow loop: calls arriving sooner are held
    max_dt_s: float | None = None        # integration cap after a gap; default 2 x sample interval
    min_weight: int = 0                  # output clamp (use >0 to keep a canary share observable)
    max_weight: int = 100
    max_step_pct: float = 25.0           # slew limit per sample; matches decisions.review_proposal
    error_clip: float = 2.0              # per-region excess saturates here: outliers cannot kick harder
    balance_tolerance: float = 0.25      # both over target and within 25% of each other => no error
    deadband: float = 0.10               # ignore pressure within 10% above target
    input_alpha: float = 0.6             # EWMA weight of the newest pressure sample (1 = no filter)
    max_age_s: float = 90.0              # freshness gate, same as engine.py / orchestrator.Policy
    min_samples: int = 3
    failback_rate_pct_per_s: float = 0.0  # 0 = never fail back automatically (engine.py behaviour)
    home_weight: int = 50                 # where failback drifts to when both regions meet target
    failback_after_s: float = 120.0       # both regions must have met target for this long first

    def __post_init__(self):
        numbers = (self.target_p95_ms, self.target_error_ratio, self.kp, self.ki, self.kd, self.sample_interval_s,
                   self.max_step_pct, self.error_clip, self.deadband, self.balance_tolerance, self.input_alpha, self.max_age_s,
                   self.failback_rate_pct_per_s, self.failback_after_s)
        if not all(_finite(n) for n in numbers):
            raise ValueError("PID configuration must be finite numbers")
        if self.target_p95_ms <= 0 or self.target_error_ratio <= 0 or self.sample_interval_s <= 0:
            raise ValueError("targets and sample interval must be positive")
        if min(self.kp, self.ki, self.kd, self.deadband, self.balance_tolerance, self.failback_rate_pct_per_s, self.failback_after_s) < 0:
            raise ValueError("gains, deadband and failback settings must not be negative")
        if not 0 <= self.min_weight < self.max_weight <= 100 or not self.min_weight <= self.home_weight <= self.max_weight:
            raise ValueError("require 0 <= min_weight <= home_weight <= max_weight <= 100 and min < max")
        if not 0 < self.max_step_pct <= 25:
            raise ValueError("max_step_pct must be in (0, 25]: review_proposal rejects larger deltas")
        if not 0 < self.input_alpha <= 1 or self.error_clip <= 0:
            raise ValueError("input_alpha must be in (0, 1] and error_clip positive")
        if self.max_dt_s is not None and (not _finite(self.max_dt_s) or self.max_dt_s < self.sample_interval_s):
            raise ValueError("max_dt_s must be at least one sample interval")


def _finite(value) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def region_pressure(obs: dict | None, cfg: PIDConfig) -> float | None:
    """Normalised pressure (1.0 = exactly on target) or None when it is UNKNOWN.

    A missing signal is never read as healthy: if either p95 or the error ratio is
    null, the remaining signal is only trusted when it already proves a breach.
    """
    if not isinstance(obs, dict) or obs.get("status") != "ok":
        return None
    age = obs.get("age_seconds")
    if not _finite(age) or not 0 <= age <= cfg.max_age_s:
        return None
    samples = obs.get("samples")
    if samples is not None and (not _finite(samples) or samples < cfg.min_samples):
        return None
    p95, err = obs.get("p95_ms"), obs.get("error_ratio")
    p95_ok = _finite(p95) and p95 >= 0
    err_ok = _finite(err) and 0 <= err <= 1
    if (p95 is not None and not p95_ok) or (err is not None and not err_ok):
        return None  # present but garbage: do not guess
    known = [p95 / cfg.target_p95_ms] if p95_ok else []
    if err_ok:
        known.append(err / cfg.target_error_ratio)
    if not known:
        return None
    worst = max(known)
    if len(known) < 2 and worst < 1:
        return None  # one signal missing and the other looks fine: health is not confirmed
    return worst


def observations_from_model_input(model_input: dict) -> dict:
    """Adapt adaptivenet.model_input.v1 to the collector-shaped dicts `step()` reads.

    Honours the pipeline's own gates: not ready_for_inference, or a region whose
    internal quality is not "fresh", becomes status "unknown" (the PID then holds).
    """
    out = {}
    ready = bool(model_input.get("ready_for_inference"))
    for region in (REGION_A, REGION_B):
        features = (model_input.get("features") or {}).get(region) or {}
        fresh = ((model_input.get("quality") or {}).get(region) or {}).get("internal") == "fresh"
        out[region] = {"status": "ok" if ready and fresh else "unknown",
                       "age_seconds": model_input.get("age_seconds"),
                       "p95_ms": features.get("http_p95_ms"),
                       "error_ratio": features.get("http_error_ratio"),
                       "source": "model_input_v1"}
    return out


@dataclass
class PIDWeightController:
    config: PIDConfig = field(default_factory=PIDConfig)
    _output: float | None = None          # float weight the integral lives in
    _last_sample: float | None = None     # time of the last accepted sample
    _prev_error: float | None = None
    _prev_prev_error: float | None = None
    _filtered: dict = field(default_factory=dict)
    _window: dict = field(default_factory=lambda: {REGION_A: [], REGION_B: []})
    _healthy_since: float | None = None

    def step(self, a: dict | None, b: dict | None, current_a_weight: int, now: float, *,
             gain_scale: float = 1.0) -> dict:
        """One control sample. `now` is a monotonic time in seconds; `gain_scale`
        is an optional advisor multiplier (see advisor_gain_scale), 0 freezes."""
        cfg = self.config
        if isinstance(current_a_weight, bool) or not isinstance(current_a_weight, int) or not 0 <= current_a_weight <= 100:
            raise ValueError("current_a_weight must be an integer 0..100")
        if not _finite(now):
            raise ValueError("now must be finite")
        # Tracking anti-windup: re-base on what is really applied (shadow mode, operator
        # override, blocked write). The fractional part survives while nothing changed.
        if self._output is None or round(self._output) != current_a_weight:
            self._output = float(current_a_weight)

        def hold(reason, **extra):
            return {"action": "hold", "reason": reason, "target_region_a_percent": current_a_weight,
                    "shadow_only": True, **extra}

        if self._last_sample is not None and now < self._last_sample:
            self._last_sample = now  # clock went backwards: resynchronise, do not integrate
            return hold("clock_regressed")
        latest = {REGION_A: region_pressure(a, cfg), REGION_B: region_pressure(b, cfg)}
        for region, value in latest.items():
            if value is not None:
                self._window[region] = (self._window[region] + [value])[-32:]
        if self._last_sample is not None and now - self._last_sample < cfg.sample_interval_s:
            return hold("between_samples")

        unknown = [r for r, p in latest.items() if p is None]
        raw = {r: (statistics.median(w) if w else None) for r, w in self._window.items()}
        for window in self._window.values():
            window.clear()
        if unknown:
            # Unknown is not zero: freeze the integral, drop derivative and filter history,
            # and restart the healthy-streak clock. _last_sample is left alone so the next
            # valid sample is evaluated immediately (dt is capped below).
            self._prev_prev_error = None
            self._filtered.clear()
            self._healthy_since = None
            return hold("unknown_telemetry:" + ",".join(unknown), pressure=latest)

        if not _finite(gain_scale) or gain_scale < 0:
            gain_scale = 1.0  # an invalid advisor never overrides the plain controller
        gain_scale = min(gain_scale, 2.0)

        for region, value in raw.items():
            previous = self._filtered.get(region)
            self._filtered[region] = value if previous is None else previous + cfg.input_alpha * (value - previous)
        excess = {r: min(cfg.error_clip, max(0.0, self._filtered[r] - 1 - cfg.deadband)) for r in raw}
        error = excess[REGION_B] - excess[REGION_A]
        if min(excess.values()) > 0 and abs(error) <= cfg.balance_tolerance * max(excess.values()):
            error = 0.0  # both regions similarly over target: moving traffic between them cannot help

        max_dt = cfg.max_dt_s if cfg.max_dt_s is not None else 2 * cfg.sample_interval_s
        dt = cfg.sample_interval_s if self._last_sample is None else min(now - self._last_sample, max_dt)
        self._last_sample = now

        both_on_target = excess[REGION_A] == 0 and excess[REGION_B] == 0
        self._healthy_since = (self._healthy_since if self._healthy_since is not None else now) if both_on_target else None

        if gain_scale == 0:
            self._prev_error, self._prev_prev_error = error, None
            return hold("advisor_freeze", pressure=raw, error=round(error, 4))

        p_term = cfg.kp * (error - (self._prev_error if self._prev_error is not None else error))
        i_term = cfg.ki * error * dt
        d_term = 0.0
        if cfg.kd and self._prev_error is not None and self._prev_prev_error is not None:
            d_term = cfg.kd * (error - 2 * self._prev_error + self._prev_prev_error) / dt
        self._prev_prev_error, self._prev_error = self._prev_error, error
        delta = gain_scale * (p_term + i_term + d_term)

        reason = "pid"
        if both_on_target and delta == 0 and cfg.failback_rate_pct_per_s > 0 \
                and now - self._healthy_since >= cfg.failback_after_s:
            gap = cfg.home_weight - self._output
            delta = math.copysign(min(abs(gap), cfg.failback_rate_pct_per_s * dt), gap)
            reason = "failback_drift"

        limited = max(-cfg.max_step_pct, min(cfg.max_step_pct, delta))
        unclamped = self._output + limited
        self._output = max(float(cfg.min_weight), min(float(cfg.max_weight), unclamped))
        saturated = limited != delta or self._output != unclamped
        target = int(round(self._output))
        detail = {"pressure": {r: round(p, 4) for r, p in raw.items()}, "error": round(error, 4),
                  "terms": {"p": round(p_term, 4), "i": round(i_term, 4), "d": round(d_term, 4)},
                  "gain_scale": gain_scale, "dt_s": round(dt, 3), "saturated": saturated}
        if target == current_a_weight:
            return hold("on_target" if both_on_target else "no_integer_change", **detail)
        return {"action": "propose_shift", "reason": reason, "target_region_a_percent": target,
                "shadow_only": True, **detail}


def advisor_gain_scale(label_a: dict | None, label_b: dict | None, *, min_confidence: float = 0.6) -> float:
    """Map an (untrusted) per-region diagnosis to a PID gain multiplier in [0, 2].

    Each argument is {"label": healthy|network|compute|critical, "confidence": 0..1}.
    The advisor can slow, freeze or speed up the loop but can NEVER move the weight
    by itself, widen the clamp, raise the slew limit or bypass the freshness gates.
    Anything invalid or low-confidence returns 1.0, i.e. the plain PID.
    """
    def read(item):
        if not isinstance(item, dict) or item.get("label") not in ("healthy", "network", "compute", "critical"):
            return None
        confidence = item.get("confidence")
        if not _finite(confidence) or not min_confidence <= confidence <= 1:
            return None
        return item["label"], float(confidence)
    a, b = read(label_a), read(label_b)
    if a is None or b is None:
        return 1.0
    labels = {a[0], b[0]}
    if a[0] != "healthy" and b[0] != "healthy":
        return 0.0   # both regions impaired: moving traffic cannot help, freeze
    if labels == {"healthy"}:
        return 0.5   # model sees nothing wrong: treat the measured breach as likely noise, go slower
    bad_label, confidence = a if a[0] != "healthy" else b
    if bad_label in ("network", "critical"):
        return 1.0 + confidence  # shifting is the right remedy for a path or hard failure
    return 1.0       # compute: shifting helps only partly; scale-out is the orchestrator's remedy


def to_proposal(decision: dict, snapshot_id: str) -> dict:
    """Wrap a step() result as adaptivenet.proposal.v1 so decisions.review_proposal can
    judge it like any model output. Confidence is 1.0 because the rule is deterministic;
    it is NOT a calibrated probability of anything."""
    shift = decision.get("action") == "propose_shift"
    proposal = {"contract_version": PROPOSAL_VERSION, "snapshot_id": snapshot_id,
                "action": "shift_traffic" if shift else "hold", "confidence": 1.0,
                "evidence": [f"pid:{decision.get('reason')}", f"pid_error={decision.get('error')}",
                             f"pressure={decision.get('pressure')}"],
                "model_id": MODEL_ID}
    if shift:
        proposal["target_region_a_percent"] = decision["target_region_a_percent"]
    return proposal
