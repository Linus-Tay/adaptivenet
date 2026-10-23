"""Prediction -> review -> decision -> action -> recovery verification, without any I/O."""
from __future__ import annotations
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adaptivenet.controller.local_snapshot import make_local_snapshot
from adaptivenet.controller.mitigation import MitigationController
from adaptivenet.ingestion.handoff import build_model_input
from adaptivenet.ingestion.decisions import review_proposal
from ml import predictor, rules
REGIONS = ("region-a", "region-b")

HOST = {'cpu_busy_pct': 5.0, 'node_load1': 0.2, 'memory_available_bytes': 6_000_000_000, 'memory_total_bytes': 8_000_000_000}

def view(latency=20.0, errors=0.0, host=HOST, samples=5):
    return {'status': 'ok', 'age_seconds': 0, 'samples': samples, 'error_ratio': errors, 'latency_stat': 'median',
            'latency_ms': None if errors >= 1 else latency, 'max_ms': None if errors >= 1 else latency * 1.5,
            'observed_at': '2026-10-23T12:00:00+00:00', 'source': 'test', 'host': host}

def snap(now, *, a_int=None, a_ext=None, b_int=None, b_ext=None, user=None):
    internal = {'region-a': a_int or view(), 'region-b': b_int or view()}
    external = {'region-a': a_ext or view(), 'region-b': b_ext or view()}
    return make_local_snapshot(internal, external, user or view(), now)

NETWORK_A = dict(a_ext=view(latency=1500))               # user path slow, region fine inside
COMPUTE_A = dict(a_int=view(latency=1200), a_ext=view(latency=1200))
CRITICAL_A = dict(a_int=view(errors=1.0), a_ext=view(errors=1.0), user=view(errors=1.0))

def controller(**kw):
    kw.setdefault("cooldown_seconds", 30)
    return MitigationController(predictor=predictor.predict, **kw)

# ---- predictor -----------------------------------------------------------------

def test_rules_separate_causes():
    now = 1000.0
    inp = lambda s: build_model_input(s, mode='internal_only', now=now)
    assert rules.classify(inp(snap(now, **NETWORK_A)))['region-a']['cause'] == 'network_degradation'
    assert rules.classify(inp(snap(now, **COMPUTE_A)))['region-a']['cause'] == 'compute_overload'
    assert rules.classify(inp(snap(now, **CRITICAL_A)))['region-a']['cause'] == 'critical_failure'
    assert rules.classify(inp(snap(now)))['region-b']['cause'] == 'healthy'

def test_app_down_behind_healthy_network_is_critical():
    # AWS case: host answers RIPE ping, application is down (app_up=0 or errors >= 0.9).
    now = 1000.0
    for a_int in (view(errors=1.0), view(errors=0.95, latency=30)):
        s = snap(now, a_int=a_int)  # external view healthy
        inp = build_model_input(s, now=now)
        verdict = rules.classify(inp)['region-a']
        assert verdict['cause'] == 'critical_failure' and verdict['confidence'] >= 0.9
        assert predictor.predict(inp)['action'] == 'failover'

def test_predictor_maps_causes_to_actions():
    now = 1000.0
    p = predictor.predict(build_model_input(snap(now, **NETWORK_A), now=now))
    assert p['action'] == 'shift_traffic' and p['target_region_a_percent'] == 0 and p['affected_region'] == 'region-a'
    assert p['contract_version'] == 'adaptivenet.proposal.v1'
    p = predictor.predict(build_model_input(snap(now, **COMPUTE_A), now=now))
    assert p['action'] == 'scale_out' and p['affected_region'] == 'region-a'
    p = predictor.predict(build_model_input(snap(now, **CRITICAL_A), now=now))
    assert p['action'] == 'failover' and p['target_region_a_percent'] == 0
    p = predictor.predict(build_model_input(snap(now), now=now))
    assert p['action'] == 'hold'

def test_predictor_investigates_when_input_not_ready():
    now = 1000.0
    stale = predictor.predict(build_model_input(snap(now, **NETWORK_A), now=now + 400))
    assert stale['action'] == 'investigate'
    missing = snap(now, a_int={'status': 'not_configured'}, **{'a_ext': view(latency=1500)})
    assert predictor.predict(build_model_input(missing, now=now))['action'] == 'investigate'

# ---- reviewer ------------------------------------------------------------------

def test_review_rejects_shift_into_unhealthy_destination():
    now = 1000.0
    s = snap(now, a_ext=view(latency=1500), b_int=view(errors=0.5))
    p = predictor.predict(build_model_input(s, now=now))
    r = review_proposal(s, {**p, 'target_region_a_percent': 75}, current_region_a_percent=100, mode='internal_only', now=now)
    assert 'destination_serving_errors' in r['rejection_reasons']

def test_review_keeps_original_defaults():
    now = 1000.0
    s = snap(now, **NETWORK_A)
    p = predictor.predict(build_model_input(s, now=now))
    r = review_proposal(s, p, current_region_a_percent=100, now=now)  # combined mode, 25 pp cap
    assert 'route_delta_above_25_percent_safety_limit' in r['rejection_reasons']
    assert s['ready_for_model'] is True  # the lab's gateway-path probes fill the external columns
    stale = review_proposal(s, p, current_region_a_percent=100, now=now + 400)
    assert 'feature_snapshot_expired_or_invalid' in stale['rejection_reasons']
    assert r['executed'] is False and r['approved_for_execution'] is False

# ---- controller ----------------------------------------------------------------

def test_network_degradation_shifts_in_bounded_steps_after_hysteresis():
    c = controller()
    for t in (0, 1):
        assert c.decide(snap(1000 + t, **NETWORK_A), 100, now=1000 + t)['decision']['action'] == 'hold'
    r = c.decide(snap(1002, **NETWORK_A), 100, now=1002)
    assert r['decision']['action'] == 'shift_traffic'
    assert r['decision']['target_region_a_percent'] == 75  # bounded step, not the model's 0
    assert r['proposal']['target_region_a_percent'] == 0
    assert r['decision']['destination'] == 'region-b'
    c.mark_applied(r, 100, {'status': 'applied'}, now=1002)
    assert c.decide(snap(1003, **NETWORK_A), 75, now=1003)['decision']['reason'] == 'cooldown'

def test_critical_failure_fails_over_fully_after_two_ticks():
    c = controller()
    assert c.decide(snap(1000, **CRITICAL_A), 100, now=1000)['decision']['action'] == 'hold'
    r = c.decide(snap(1001, **CRITICAL_A), 100, now=1001)
    assert r['decision']['action'] == 'failover' and r['decision']['target_region_a_percent'] == 0

def test_compute_overload_scales_out_and_does_not_move_traffic():
    c = controller(scale_available=True)
    for t in range(2):
        c.decide(snap(1000 + t, **COMPUTE_A), 100, now=1000 + t)
    r = c.decide(snap(1002, **COMPUTE_A), 100, now=1002)
    assert r['decision']['action'] == 'scale_out' and r['decision']['target_region_a_percent'] == 100

def test_scale_out_without_adapter_relieves_by_shifting():
    c = controller(scale_available=False)
    for t in range(3):
        r = c.decide(snap(1000 + t, **COMPUTE_A), 100, now=1000 + t)
    assert r['proposal']['action'] == 'shift_traffic' and 'controller:no_scale_adapter' in r['proposal']['evidence']
    assert r['decision']['action'] == 'shift_traffic' and r['decision']['target_region_a_percent'] == 75
    # The reviewer itself still refuses a bare scale_out when no adapter exists.
    s = snap(1003, **COMPUTE_A)
    p = predictor.predict(build_model_input(s, now=1003))
    assert 'kubernetes_scale_adapter_not_implemented' in review_proposal(s, p, now=1003)['rejection_reasons']

def test_shift_steps_continue_until_users_recover():
    c = controller(verify_after_seconds=2, verify_healthy_ticks=2)
    weight = 100
    for t in range(3):
        r = c.decide(snap(1000 + t, **NETWORK_A), weight, now=1000 + t)
    c.mark_applied(r, weight, None, now=1002); weight = 75
    # users still slow while most traffic stays on region-a: two checks, then the next step, no cooldown wait
    for t in (1005, 1006):
        r = c.decide(snap(t, a_ext=view(latency=1500), user=view(latency=1500)), weight, now=t)
    assert r['decision']['action'] == 'shift_traffic' and r['decision']['target_region_a_percent'] == 50
    assert c.journal[0]['verification']['status'] == 'superseded'
    c.mark_applied(r, weight, None, now=1006); weight = 50
    for t in (1009, 1010):
        r = c.decide(snap(t, **NETWORK_A), weight, now=t)  # users fine now
    assert c.journal[-1]['verification']['status'] == 'recovered'
    assert r['decision']['action'] == 'hold'

def test_scale_out_not_effective_escalates_to_shift():
    c = controller(scale_available=True, verify_after_seconds=5, verify_deadline_seconds=20, cooldown_seconds=1)
    for t in range(3):
        r = c.decide(snap(1000 + t, **COMPUTE_A), 100, now=1000 + t)
    assert r['decision']['action'] == 'scale_out'
    c.mark_applied(r, 100, {'status': 'scaled'}, now=1002)
    # Region stays overloaded past the deadline: verification fails, escalation flag set.
    for t in (1010, 1020, 1030):
        r = c.decide(snap(t, **COMPUTE_A), 100, now=t)
    assert c.journal[0]['verification']['status'] == 'not_recovered'
    assert 'region-a' in c.escalated
    for t in (1031, 1032, 1033):
        r = c.decide(snap(t, **COMPUTE_A), 100, now=t)
    assert r['decision']['action'] == 'shift_traffic' and r['decision']['target_region_a_percent'] == 75
    assert 'controller:scale_out_not_effective' in r['proposal']['evidence']

def test_recovery_verified_from_user_path():
    c = controller(verify_after_seconds=5)
    for t in range(3):
        r = c.decide(snap(1000 + t, **NETWORK_A), 100, now=1000 + t)
    c.mark_applied(r, 100, {'status': 'applied'}, now=1002)
    assert c.journal[0]['verification']['status'] == 'pending'
    # After the shift, users get healthy responses (unpinned probes), even though region-a's pinned path is still slow.
    for t in (1010, 1011):
        c.decide(snap(t, **NETWORK_A), 0, now=t)
    v = c.journal[0]['verification']
    assert v['status'] == 'recovered' and v['recovered_after_seconds'] == 9.0

def test_never_shifts_into_unhealthy_region():
    c = controller()
    both = dict(a_ext=view(latency=1500), b_ext=view(latency=1500))
    for t in range(5):
        r = c.decide(snap(1000 + t, **both), 100, now=1000 + t)
        assert r['decision']['action'] == 'hold'

def test_unknown_evidence_never_acts():
    c = controller()
    for t in range(5):
        s = snap(1000 + t, a_ext=view(latency=1500), b_int={'status': 'stale'})
        assert c.decide(s, 100, now=1000 + t)['decision']['action'] == 'hold'

def test_no_automatic_failback_by_default_and_opt_in_failback():
    c = controller(cooldown_seconds=0)
    for t in range(3):
        r = c.decide(snap(1000 + t, **NETWORK_A), 100, now=1000 + t)
    c.mark_applied(r, 100, None, now=1002)
    for t in range(1010, 1030):
        c.decide(snap(t), 75, now=t)
    assert len(c.journal) == 1  # recovered, but weight stays at 75
    c.auto_failback, c.failback_after_ticks = True, 3
    r = None
    for t in range(1030, 1036):
        r = c.decide(snap(t), 75, now=t)
    assert r['decision']['action'] == 'failback' and r['decision']['target_region_a_percent'] == 100

def test_journal_records_model_and_actuation():
    c = controller()
    for t in range(3):
        r = c.decide(snap(1000 + t, **NETWORK_A), 100, now=1000 + t)
    e = c.mark_applied(r, 100, {'status': 'applied', 'region_a_percent': 75}, now=1002)
    assert e['model_id'] == 'dual_view_rules_v1' and e['proposal_action'] == 'shift_traffic'
    assert e['from_region_a_percent'] == 100 and e['to_region_a_percent'] == 75
    assert e['snapshot_id'].startswith('features-')

# ---- trained-model path ----------------------------------------------------------

def test_trained_model_bundle_drives_the_same_proposal_mapping(tmp_path, monkeypatch):
    sklearn = pytest.importorskip('sklearn')
    import json, pickle
    from ml import train
    # Tiny synthetic training set exercising the plumbing only; it says nothing about real accuracy.
    rows = []
    for t in range(6):
        for label, kw in (('healthy', {}), ('region-a:network_degradation', NETWORK_A),
                          ('region-a:compute_overload', COMPUTE_A), ('region-a:critical_failure', CRITICAL_A)):
            s = snap(1000 + t, **kw)
            rows.append({'features': s['regions']['region-a']['features'] and {r: s['regions'][r]['features'] for r in REGIONS}, 'fault_label': label})
    data = tmp_path / 'rows.jsonl'
    data.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
    out = tmp_path / 'model.pkl'
    assert train.main(['--data', str(data), '--out', str(out), '--model-id', 'test_model']) == 0
    monkeypatch.setenv(predictor.MODEL_ENV, str(out))
    predictor._loaded.clear()
    p = predictor.predict(build_model_input(snap(2000, **NETWORK_A), now=2000))
    assert p['model_id'] == 'test_model' and p['action'] == 'shift_traffic' and p['target_region_a_percent'] == 0
    p = predictor.predict(build_model_input(snap(2000), now=2000))
    assert p['action'] == 'hold'
    monkeypatch.delenv(predictor.MODEL_ENV)
    predictor._loaded.clear()
