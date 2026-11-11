#!/usr/bin/env python3
"""One-command, read-only native 3-model shadow and independent reviewer check.

Uses only genuine archived features + witness observations; no AWS/Docker calls,
no telemetry modification, no synthetic labels, no mitigation execution.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from step5_finish.finalize_step5 import GROUPS, STEP4, preflight, read_json

OUT = ROOT / "data/ml-results/native/step5-network-shadow"
SNAPSHOTS = ROOT / "data/ml-archive/feature_snapshots.jsonl"
WITNESSES = ROOT / "data/ml-archive/http_witness.jsonl"
MAX_SNAPSHOT_AGE = 180.0  # existing shadow_predict limit; not relaxed


def load_recent_snapshots(path: Path, count: int, now: float) -> list[dict]:
    """Select newest DISTINCT immutable records within the predictor freshness gate."""
    if not path.is_file():
        raise FileNotFoundError(f"Missing original snapshot archive: {path}")
    selected: dict[str, dict] = {}
    latest_seen = None
    with path.open(encoding="utf-8-sig") as stream:
        for lineno, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                snap = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid archive JSON at line {lineno}: {exc}") from exc
            if not isinstance(snap, dict) or snap.get('contract_version') != 'adaptivenet.features.v1':
                continue
            ident = snap.get('snapshot_id')
            stamp = snap.get('computed_at_unix')
            if not isinstance(ident, str) or not ident.startswith('features-'):
                continue
            if isinstance(stamp, bool) or not isinstance(stamp,(float,int)) or not math.isfinite(stamp):
                continue
            latest_seen = max(latest_seen or float('-inf'), stamp)
            if 0 <= now - stamp <= MAX_SNAPSHOT_AGE:
                selected[ident] = snap
    fresh = sorted(selected.values(), key=lambda x: (x['computed_at_unix'], x['snapshot_id']))
    if not fresh:
        age = 'unknown' if latest_seen is None else f'{now-latest_seen:.1f}s'
        raise ValueError('No genuinely fresh snapshots (<=180s). Latest archive snapshot age: '+age+'. '
                         'Run your existing read-only archive watcher; do not edit timestamps.')
    return fresh[-count:]


def load_http_asof(path: Path, cutoff: float) -> list[dict]:
    """Only observations completed before the original snapshot, within 15 minutes."""
    from ml.team_starter.verified_labels import unix
    if not path.is_file():
        raise FileNotFoundError(f'Missing HTTP witness archive: {path}')
    events = []
    with path.open(encoding='utf-8-sig') as stream:
        for lineno, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f'Invalid HTTP archive JSON at line {lineno}: {exc}') from exc
            if not isinstance(event, dict) or event.get('scope') not in ('region-a','region-b','cloudfront'):
                continue
            try:
                stamp = unix(event.get('timestamp'))
            except (ValueError, TypeError):
                continue
            if cutoff - 900 <= stamp <= cutoff:
                events.append(event)
    return events


def run_sample(snap: dict, paths: dict, witness: Path, now: float) -> dict:
    from adaptivenet.ingestion.handoff import build_model_input
    from adaptivenet.ingestion.fast_signals import build_fast_signals
    from ml.team_starter.shadow import shadow_predict, shadow_review

    original_id = snap['snapshot_id']
    stamp = snap['computed_at_unix']
    age = now - stamp
    if not 0 <= age <= MAX_SNAPSHOT_AGE:
        raise ValueError(f'{original_id}: stale snapshot: {age:.1f}s')
    if snap.get('ready_for_model') is not True or snap.get('quality_reasons') or snap.get('source_errors'):
        raise ValueError(f'{original_id}: source snapshot not ready for model; no readiness overrides')

    # Use the team's existing converter, NEVER fabricate missing values or timestamps.
    # Set "now" to the actual original decision cutoff to preserve the source's
    # point-in-time semantics. shadow_predict independently enforces live age.
    slow = build_model_input(snap, mode='combined', now=stamp)
    if slow.get('contract_version') != 'adaptivenet.model_input.v1' or slow.get('snapshot_id') != original_id:
        raise ValueError('Model converter returned wrong contract/snapshot ID')
    if slow.get('computed_at_unix') != stamp:
        raise ValueError('Model converter changed original snapshot timestamp')
    events = load_http_asof(witness, stamp)
    fast = build_fast_signals(events, now=stamp)
    if fast.get('contract_version') != 'adaptivenet.fast_signals.v1':
        raise ValueError('Invalid fast-signals contract')
    payload = {'contract_version':'adaptivenet.detection_input.v1', 'slow':slow, 'fast':fast}
    results = {}
    for group in GROUPS:
        result = {'status':'error', 'effective_action':'hold', 'executed':False,
                  'approved_for_execution':False}
        try:
            proposal = shadow_predict(payload, paths[group])
            if proposal.get('contract_version') != 'adaptivenet.proposal.v1':
                raise ValueError('Wrong proposal contract')
            if proposal.get('snapshot_id') != original_id:
                raise ValueError('Wrong model proposal snapshot ID')
            if proposal.get('action') not in ('hold','investigate'):
                raise ValueError('Unsafe proposal action')
            conditions = [v.split(':',1)[1] for v in proposal.get('evidence', [])
                          if isinstance(v,str) and v.startswith('shadow_condition:')]
            review = shadow_review(snap, proposal) # genuine independent reviewer
            if review.get('review_version') != 'adaptivenet.decision_review.v1':
                raise ValueError('Independent reviewer wrong contract')
            result.update({'status': 'trained_prediction' if conditions else 'abstained',
                           'condition': conditions[0] if conditions else None,
                           'confidence': proposal.get('confidence'),
                           'proposal':proposal, 'independent_review':review,
                           'reviewer_invoked':True,
                           'reviewer_policy_valid':review['policy_valid'] is True})
        except Exception as exc:
            result.update({'error': f'{type(exc).__name__}: {exc}',
                           'reviewer_invoked': 'independent_review' in result,
                           'reviewer_policy_valid':False})
        results[group] = result
    return {
        'observed_at_utc': datetime.now(timezone.utc).isoformat(),
        'snapshot_id':original_id,
        'snapshot_computed_at_unix': stamp,
        'snapshot_age_seconds_at_selection':age,
        'archive_original_snapshot_sha256':hashlib.sha256(json.dumps(snap, sort_keys=True).encode()).hexdigest(),
        'source_snapshots_file':str(SNAPSHOTS.relative_to(ROOT)),
        'source_witness_file':str(witness.relative_to(ROOT)) if witness.is_relative_to(ROOT) else str(witness),
        'http_events_asof':len(events),
        'original_ready_for_model':snap.get('ready_for_model'),
        'ready_for_automatic_action':snap.get('ready_for_automatic_action'),
        'fast_ready':fast.get('ready_for_fast_assessment'),
        'models':results,
        'shadow_only':True, 'automatic_mitigation_enabled':False,
        'executed':False,'approved_for_execution':False,'effective_action':'hold',
        'observation_type':'recent_archived_snapshot_replay_not_prospective',
    }


def main(argv=None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshots',type=Path,default=SNAPSHOTS)
    parser.add_argument('--witness',type=Path,default=WITNESSES)
    parser.add_argument('--count',type=int,default=2,help='Newest distinct snapshots within 180s, max 3')
    args=parser.parse_args(argv)
    if not 1 <= args.count <= 3:
        parser.error('--count must be 1..3')
    paths = preflight(read_json(STEP4)) # verify each SHA256 matches Step 4
    now = time.time()
    snaps = load_recent_snapshots(args.snapshots,args.count, now)
    entries=[]
    for snap in snaps:
        entries.append(run_sample(snap, paths, args.witness, time.time()))
        row=entries[-1]
        print(row['snapshot_id'], f"age={row['snapshot_age_seconds_at_selection']:.1f}s",
              ' '.join(f'{g}={row["models"][g]["status"]}' for g in GROUPS))
    every_reviewed = all(e['models'][g].get('reviewer_policy_valid') for e in entries for g in GROUPS)
    every_predicted = all(e['models'][g]['status']=='trained_prediction' for e in entries for g in GROUPS)
    all_hold = all(e['models'][g].get('effective_action')=='hold' and
                   e['models'][g].get('executed') is False and
                   e['models'][g].get('approved_for_execution') is False
                   for e in entries for g in GROUPS)
    report={
        'phase':'step5_native_shadow_review',
        'status': ('SHADOW_INFERENCE_AND_REVIEW_VERIFIED_LIMITED' if every_reviewed and every_predicted and all_hold
                   else 'SHADOW_PARTIAL_CHECK_REVIEW_OUTPUTS'),
        'distinct_snapshots':len({e['snapshot_id'] for e in entries}),
        'samples':len(entries), 'trained_predictions':sum(e['models'][g]['status']=='trained_prediction' for e in entries for g in GROUPS),
        'total_model_invocations':len(entries)*len(GROUPS),
        'independent_reviewer_valid_for_all':bool(every_reviewed),
        'all_actions_hold_and_not_executed':bool(all_hold),
        'model_groups':list(GROUPS),
        'automatic_mitigation_enabled':False,
        'prospective_healthy_false_alarm_rate':None,
        'step4_heldout_healthy_false_alarm_rate':None,
        'observation_type':'recent_archived_snapshot_replay_not_prospective',
        'limitations':['Recent archival replay is not a prospectively collected independent sample',
                       'No prospective healthy/fault ground truth established',
                       'Held-out Step 4 false alarms still not estimable',
                       'No automatic mitigation; production readiness not established']
    }
    OUT.mkdir(parents=True,exist_ok=True)
    # New report names, so do not overwrite the prior shadow-report history.
    (OUT/'STEP5_NATIVE_SHADOW_OBSERVATIONS.jsonl').write_text(''.join(json.dumps(e,sort_keys=True,allow_nan=False)+'\n' for e in entries),encoding='utf-8')
    (OUT/'STEP5_NATIVE_SHADOW_REPORT.json').write_text(json.dumps(report,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    (OUT/'STEP5_NATIVE_SHADOW_REPORT.md').write_text('\n'.join([
        '# AdaptiveNet Step 5 — genuine recent snapshots, native model, independent reviewer',
        '', f"Status: **{report['status']}**",
        f"Distinct snapshots: {report['distinct_snapshots']}",
        f"Trained predictions: {report['trained_predictions']}/{report['total_model_invocations']}",
        f"All reviewer policies valid: {report['independent_reviewer_valid_for_all']}",
        f"No execution, all actions hold: {report['all_actions_hold_and_not_executed']}",
        '', 'IMPORTANT: recent archived replay, not prospective monitoring; no healthy false-alarm rate.',
        'No routing, Kubernetes, Terraform, or AWS changes.',
        '', 'Per-arm details in STEP5_NATIVE_SHADOW_OBSERVATIONS.jsonl.', '']), encoding='utf-8')
    print('STEP 5 STATUS:', report['status'])
    print(f"TRAINED PREDICTIONS: {report['trained_predictions']}/{report['total_model_invocations']}")
    print('INDEPENDENT REVIEW VALID:',report['independent_reviewer_valid_for_all'])
    print('SAFE HOLD ONLY:',report['all_actions_hold_and_not_executed'])
    print('SAVED:', OUT/'STEP5_NATIVE_SHADOW_REPORT.md')
    # Success for acceptance only when actual trained predictions AND reviewer check pass.
    return 0 if report['status']=='SHADOW_INFERENCE_AND_REVIEW_VERIFIED_LIMITED' else 2

if __name__=='__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('STEP 5 STOPPED SAFELY:',type(exc).__name__+':',str(exc),file=sys.stderr)
        raise SystemExit(2)
