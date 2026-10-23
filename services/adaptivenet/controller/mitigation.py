"""Model-driven mitigation controller: prediction -> validated decision -> action -> verified recovery.

Per tick (`decide`), with no I/O of its own:

  1. build_model_input(snapshot)            telemetry contract (ingestion/handoff.py)
  2. predictor(model_input) -> proposal     ml/predictor.py: hold / investigate / shift_traffic / scale_out / failover
  3. review_proposal(...)                   independent validation (ingestion/decisions.py); a rejected proposal never acts
  4. verification of earlier actions        has the user path / the region recovered? (closes journal entries)
  5. policy                                 hysteresis (N identical proposals), cooldown, bounded route step,
                                            never move traffic into a region the model does not call healthy,
                                            escalation when scale-out did not help, optional failback
  -> decision {action, region, target_region_a_percent, reason}

The caller (controller/routes.py) actuates when automation is enabled and then calls
`mark_applied`, which starts the cooldown and the recovery-verification window.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import importlib
import os
import sys
import time
from pathlib import Path
from typing import Callable

from ..ingestion.handoff import build_model_input
from ..ingestion.decisions import review_proposal, PASSIVE

REGIONS = ('region-a', 'region-b')
ACTIVE = ('shift_traffic', 'failover', 'scale_out')


def load_predictor(spec: str | None = None) -> Callable[[dict], dict]:
    """Resolve 'module:function' (default ml.predictor:predict); the repo root is added
    to sys.path when `ml` is not installed as a package (uvicorn with PYTHONPATH=services)."""
    spec = spec or os.getenv('ADAPTIVENET_PREDICTOR', 'ml.predictor:predict')
    module_name, _, function = spec.partition(':')
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        root = Path(__file__).resolve().parents[3]
        if str(root) not in sys.path:
            sys.path.append(str(root))
        module = importlib.import_module(module_name)
    return getattr(module, function or 'predict')


def other(region: str) -> str:
    return REGIONS[1 - REGIONS.index(region)]


def away_from(region: str) -> int:
    """Route weight (region-a percent) that sends all traffic to the other region."""
    return 0 if region == 'region-a' else 100


def user_path_healthy(snapshot: dict, assessment: dict, destination: str | None,
                      error_pct: float = 5.0, latency_ms: float = 800.0) -> tuple[bool | None, str]:
    """Recovery signal for routing actions: what users get now (global view), falling back
    to the destination region's own assessment when no global metrics exist."""
    cf = (snapshot.get('cloudfront') or {}).get('features') or {}
    errors = cf.get('cloudfront_5xx_pct')
    if errors is not None:
        # Worst probe, not the median: a minority of users still routed to a slow
        # region must keep the mitigation stepping. CloudFront exposes only error rates.
        latency = cf.get('user_path_latency_max_ms', cf.get('user_path_latency_p50_ms'))
        ok = errors < error_pct and (latency is None or latency < latency_ms)
        return ok, f'user_path_5xx_pct={errors} latency_worst_ms={latency}'
    if destination and destination in assessment:
        cause = assessment[destination]['cause']
        return cause == 'healthy', f'no global metrics; destination {destination} is {cause}'
    return None, 'no recovery signal available'


@dataclass
class MitigationController:
    predictor: Callable[[dict], dict] | None = None
    mode: str = 'combined'  # combined (user-path + in-region views) | internal_only
    consecutive_required: int = 3
    critical_required: int = 2
    cooldown_seconds: float = 30
    route_step_percent: int = 25
    verify_after_seconds: float = 10
    verify_deadline_seconds: float = 90
    verify_healthy_ticks: int = 2
    scale_available: bool = False
    auto_failback: bool = False
    failback_after_ticks: int = 6
    # state
    streak: tuple = (None, 0)                       # ((action, region), count)
    last_action: float = field(default=float('-inf'))
    baseline_region_a_percent: int | None = None
    healthy_ticks: int = 0
    escalated: set = field(default_factory=set)     # regions where scale_out did not recover
    journal: list = field(default_factory=list)
    _seq: int = 0

    def __post_init__(self):
        if self.predictor is None:
            self.predictor = load_predictor()

    # ---- recovery verification -------------------------------------------------
    @property
    def pending(self) -> list[dict]:
        return [e for e in self.journal if e['verification']['status'] == 'pending']

    def _verify(self, snapshot: dict, assessment: dict, now: float) -> list[dict]:
        closed = []
        for entry in self.pending:
            v = entry['verification']
            if now < entry['at'] + self.verify_after_seconds:
                continue
            if entry['action'] == 'scale_out':
                cause = assessment[entry['region']]['cause']
                healthy, detail = cause == 'healthy', f"{entry['region']} is {cause}"
            else:
                healthy, detail = user_path_healthy(snapshot, assessment, entry.get('destination'))
            v['checked_at'] = now
            v['detail'] = detail
            v['checks'] += 1
            if healthy:
                v['healthy_ticks'] += 1
            else:
                v['healthy_ticks'] = 0
            if v['healthy_ticks'] >= self.verify_healthy_ticks:
                v.update(status='recovered', recovered_after_seconds=round(now - entry['at'], 1))
                self.escalated.discard(entry['region'])
                closed.append(entry)
            elif now > entry['at'] + self.verify_deadline_seconds:
                v.update(status='not_recovered')
                if entry['action'] == 'scale_out':
                    self.escalated.add(entry['region'])  # next: move traffic instead
                closed.append(entry)
        return closed

    # ---- decision ---------------------------------------------------------------
    def decide(self, snapshot: dict, current_a_weight: int, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        model_input = build_model_input(snapshot, mode=self.mode, now=now)
        proposal = self.predictor(model_input)
        assessment = proposal.get('assessment') or {r: {'cause': 'unknown'} for r in REGIONS}
        closed = self._verify(snapshot, assessment, now)
        action, region = proposal.get('action'), proposal.get('affected_region')
        target = proposal.get('target_region_a_percent')

        # Substitution: no way to add capacity here, or scale-out did not restore the region
        # within its verification window -> relieve the region by moving traffic instead.
        if action == 'scale_out' and (region in self.escalated or not self.scale_available):
            why = 'controller:scale_out_not_effective' if region in self.escalated else 'controller:no_scale_adapter'
            action, target = 'shift_traffic', away_from(region)
            proposal = {**proposal, 'action': action, 'target_region_a_percent': target,
                        'evidence': list(proposal.get('evidence', [])) + [why]}

        bounded = proposal
        if action in ('shift_traffic', 'failover') and isinstance(target, int):
            step = 100 if action == 'failover' else self.route_step_percent
            bounded_target = max(current_a_weight - step, target) if target < current_a_weight else min(current_a_weight + step, target)
            bounded = {**proposal, 'target_region_a_percent': bounded_target}
        review = review_proposal(snapshot, bounded, current_region_a_percent=current_a_weight, mode=self.mode,
                                 max_route_delta=100 if action == 'failover' else self.route_step_percent,
                                 scale_adapter_available=self.scale_available, now=now)

        key = (action, region) if action in ACTIVE else None
        self.streak = (key, self.streak[1] + 1) if key and key == self.streak[0] else (key, 1 if key else 0)
        all_healthy = all(assessment[r]['cause'] == 'healthy' for r in REGIONS)
        self.healthy_ticks = self.healthy_ticks + 1 if all_healthy else 0

        decision = {'action': 'hold', 'region': region, 'target_region_a_percent': current_a_weight,
                    'reason': 'no actionable proposal', 'streak': self.streak[1],
                    'required_streak': self.critical_required if action == 'failover' else self.consecutive_required}
        result = {'snapshot_id': snapshot.get('snapshot_id'), 'model_input_ready': model_input['ready_for_inference'],
                  'quality_reasons': model_input['quality_reasons'], 'assessment': assessment,
                  'proposal': proposal, 'review': review, 'decision': decision,
                  'verification_closed': [{'seq': e['seq'], 'action': e['action'], **e['verification']} for e in closed],
                  'pending_verification': [e['seq'] for e in self.pending]}
        if action in PASSIVE or action is None:
            decision['reason'] = f'model proposes {action}'
            self._maybe_failback(decision, current_a_weight, assessment)
            return result
        if not review['policy_valid']:
            if review['rejection_reasons'] == ['route_weight_unchanged']:
                decision['reason'] = f'already fully routed away from {region}; nothing more to move'
            else:
                decision['reason'] = 'proposal rejected by review: ' + ', '.join(review['rejection_reasons'])
            return result
        continuing = self._continuing(action, region)
        if continuing is None:
            if now - self.last_action < self.cooldown_seconds:
                decision['reason'] = 'cooldown'
                return result
            if self.streak[1] < decision['required_streak']:
                decision['reason'] = f"waiting for {decision['required_streak']} consecutive identical proposals"
                return result
        if action in ('shift_traffic', 'failover'):
            if assessment[other(region)]['cause'] != 'healthy':
                decision['reason'] = f"destination {other(region)} is {assessment[other(region)]['cause']}; not moving traffic into it"
                return result
            decision.update(action=action, target_region_a_percent=bounded['target_region_a_percent'],
                            destination=other(region),
                            reason=(f"users still degraded after step {continuing['seq']}; next {self.route_step_percent} pp to {other(region)}"
                                    if continuing else
                                    f"{region} {assessment[region]['cause']} for {self.streak[1]} ticks; "
                                    f"{'full failover' if action == 'failover' else f'step {self.route_step_percent} pp'} to {other(region)}"))
            if continuing:
                continuing['verification'].update(status='superseded', detail='users still degraded; next step taken')
        else:  # scale_out
            if self.pending and any(e['region'] == region and e['action'] == 'scale_out' for e in self.pending):
                decision['reason'] = 'scale-out already in progress; awaiting verification'
                return result
            decision.update(action='scale_out', reason=f"{region} compute_overload for {self.streak[1]} ticks; adding capacity in place")
        return result

    def _continuing(self, action: str | None, region: str | None) -> dict | None:
        """A bounded shift step whose verification has checked at least `verify_healthy_ticks`
        times and never saw users recover: take the next step without a fresh streak/cooldown."""
        if action != 'shift_traffic' or not self.journal:
            return None
        last = self.journal[-1]
        v = last['verification']
        if (last['action'] == 'shift_traffic' and last['region'] == region and v['status'] == 'pending'
                and v['checks'] >= self.verify_healthy_ticks and v['healthy_ticks'] == 0):
            return last
        return None

    def _maybe_failback(self, decision: dict, current: int, assessment: dict):
        if not self.auto_failback or self.baseline_region_a_percent is None or current == self.baseline_region_a_percent:
            return
        if self.pending or self.healthy_ticks < self.failback_after_ticks:
            decision['reason'] += f'; failback waits for {self.failback_after_ticks} healthy ticks ({self.healthy_ticks}) and no pending verification'
            return
        if time.time() - self.last_action < self.cooldown_seconds:
            return
        baseline = self.baseline_region_a_percent
        step = self.route_step_percent
        target = max(current - step, baseline) if baseline < current else min(current + step, baseline)
        destination = 'region-a' if target > current else 'region-b'
        decision.update(action='failback', region=destination, destination=destination, target_region_a_percent=target,
                        reason=f'both regions healthy for {self.healthy_ticks} ticks; restoring baseline {baseline}% in {step} pp steps')

    # ---- bookkeeping -------------------------------------------------------------
    def mark_applied(self, result: dict, current_a_weight: int, actuation: dict | None, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        decision = result['decision']
        if self.baseline_region_a_percent is None:
            self.baseline_region_a_percent = current_a_weight
        self._seq += 1
        entry = {'seq': self._seq, 'at': now, 'action': decision['action'], 'region': decision.get('region'),
                 'destination': decision.get('destination'),
                 'from_region_a_percent': current_a_weight, 'to_region_a_percent': decision['target_region_a_percent'],
                 'reason': decision['reason'], 'snapshot_id': result.get('snapshot_id'),
                 'model_id': (result.get('proposal') or {}).get('model_id'),
                 'proposal_action': (result.get('proposal') or {}).get('action'), 'actuation': actuation,
                 'verification': {'status': 'pending', 'checks': 0, 'healthy_ticks': 0, 'checked_at': None, 'detail': None}}
        self.journal.append(entry)
        self.last_action = now
        self.streak = (None, 0)
        return entry

    def reset(self):
        self.streak, self.last_action, self.healthy_ticks = (None, 0), float('-inf'), 0
        self.baseline_region_a_percent, self._seq = None, 0
        self.escalated.clear()
        self.journal.clear()
