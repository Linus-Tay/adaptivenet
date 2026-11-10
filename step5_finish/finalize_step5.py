#!/usr/bin/env python3
"""Step 5: read-only, three-model network shadow observation. Never routes traffic."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
BASE = ROOT / 'data' / 'ml-results' / 'native'
STEP4 = BASE / 'step4-network-evaluation' / 'step4-evaluation.json'
MODELS = BASE / 'step3-network-models'
OUT = BASE / 'step5-network-shadow'
GROUPS = ('aws_only', 'aws_ripe', 'aws_ripe_http')


def read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def preflight(report):
    if report.get('evaluation_status') != 'completed_with_explicit_heldout_healthy_limitation':
        raise RuntimeError('Step 4 verification missing or changed')
    models = report.get('models', {})
    if set(models) != set(GROUPS):
        raise RuntimeError('Expected three models from Step 4')
    paths = {}
    for group in GROUPS:
        record = models[group]
        path = MODELS / Path(record['model_file'].replace('\\', '/')).name
        if not path.is_file() or path.resolve().parent != MODELS.resolve():
            raise RuntimeError(f'Missing trusted local model for {group}: {path}')
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != record['model_sha256']:
            raise RuntimeError(f'Model SHA-256 changed since Step 4: {group}')
        paths[group] = path
    return paths


def load_input(path, url):
    if path:
        obj = read_json(path)
    else:
        # localhost only: no AWS, no remote endpoint and no control API.
        from urllib.parse import urlsplit
        parsed = urlsplit(url)
        if parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1'):
            raise ValueError('Only local read-only HTTP input is allowed')
        with urlopen(url, timeout=8) as response:
            if response.status != 200:
                raise RuntimeError(f'Detection endpoint returned HTTP {response.status}')
            obj = json.loads(response.read(5_000_001))
    if not isinstance(obj, dict) or obj.get('contract_version') not in ('adaptivenet.model_input.v1', 'adaptivenet.detection_input.v1'):
        raise ValueError('Expected model_input.v1 or detection_input.v1')
    slow = obj.get('slow') if obj.get('contract_version') == 'adaptivenet.detection_input.v1' else obj
    if not isinstance(slow, dict) or slow.get('contract_version') != 'adaptivenet.model_input.v1':
        raise ValueError('Invalid slow model input')
    if not isinstance(slow.get('snapshot_id'), str) or not slow['snapshot_id'].startswith('features-'):
        raise ValueError('Invalid feature snapshot id')
    return obj, slow


def infer_three(payload, paths, predictor=None):
    if predictor is None:
        from ml.team_starter.shadow import shadow_predict
        predictor = shadow_predict
    sid = (payload.get('slow') or {}).get('snapshot_id') if payload.get('contract_version') == 'adaptivenet.detection_input.v1' else payload.get('snapshot_id')
    outcomes = {}
    for group in GROUPS:
        try:
            proposal = predictor(payload, paths[group])
            if not isinstance(proposal, dict) or proposal.get('contract_version') != 'adaptivenet.proposal.v1':
                raise ValueError('Wrong proposal contract')
            if proposal.get('snapshot_id') != sid:
                raise ValueError('Wrong snapshot identity')
            action = proposal.get('action')
            if action not in ('hold', 'investigate'):
                raise ValueError('Unsafe action rejected')
            confidence = proposal.get('confidence')
            if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                    or not math.isfinite(confidence) or not 0 <= confidence <= 1):
                raise ValueError('Invalid confidence')
            notes = proposal.get('evidence', [])
            if not isinstance(notes, list) or not all(isinstance(v, str) for v in notes):
                raise ValueError('Invalid evidence')
            conditions = [v.split(':', 1)[1] for v in notes if v.startswith('shadow_condition:')]
            outcomes[group] = {
                'status': 'prediction' if conditions else 'abstain_or_baseline',
                'condition': conditions[0] if conditions else None,
                'confidence': float(confidence),
                'proposal': proposal,
                'approved_for_execution': False,
                'executed': False,
                'effective_action': 'hold',
                'note': 'No independent reviewer invoked: no original feature snapshot supplied',
            }
        except Exception as exc:
            # Fail closed per arm: log failure, no traffic changes, no fabricated prediction.
            outcomes[group] = {
                'status': 'error', 'condition': None, 'confidence': None,
                'error': f'{type(exc).__name__}: {exc}',
                'approved_for_execution': False, 'executed': False, 'effective_action': 'hold'
            }
    labels = [v['condition'] for v in outcomes.values() if v['condition'] is not None]
    return outcomes, (len(set(labels)) > 1 if len(labels) >= 2 else None)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, help='Existing real current detection_input.v1 JSON; otherwise query local API')
    parser.add_argument('--url', default='http://127.0.0.1:8000/v1/detection/input')
    parser.add_argument('--samples', type=int, default=1)
    parser.add_argument('--interval', type=float, default=5)
    args = parser.parse_args(argv)
    if not 1 <= args.samples <= 100 or not 0 <= args.interval <= 3600:
        parser.error('samples must be 1-100 and interval 0-3600 seconds')
    paths = preflight(read_json(STEP4))
    OUT.mkdir(parents=True, exist_ok=True)
    entries = []
    for i in range(args.samples):
        if i:
            time.sleep(args.interval)
        payload, slow = load_input(args.input, args.url)
        outcomes, disagree = infer_three(payload, paths)
        sample = {
            'observed_at_utc': datetime.now(timezone.utc).isoformat(),
            'snapshot_id': slow['snapshot_id'], 'snapshot_computed_at_unix': slow.get('computed_at_unix'),
            'source': str(args.input) if args.input else args.url,
            'model_outputs': outcomes, 'prediction_disagreement': disagree,
            'shadow_only': True, 'automatic_mitigation_enabled': False,
            'approved_for_execution': False, 'executed': False, 'effective_action': 'hold',
        }
        entries.append(sample)
        print(f"sample {i+1}/{args.samples}: {slow['snapshot_id']} " + ' '.join(f"{g}={outcomes[g]['status']}:{outcomes[g]['condition']}" for g in GROUPS))
    with (OUT / 'step5-shadow-observations.jsonl').open('a', encoding='utf-8') as f:
        for record in entries:
            f.write(json.dumps(record, allow_nan=False, sort_keys=True) + '\n')
    allrows = [json.loads(line) for line in (OUT / 'step5-shadow-observations.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    distinct_ids = {x['snapshot_id'] for x in allrows}
    summary = {
        'phase': 'step5_shadow_only', 'samples_this_run': len(entries),
        'total_logged_samples': len(allrows), 'distinct_snapshot_ids': len(distinct_ids),
        'by_group': {g: dict(Counter(x['model_outputs'][g]['status'] for x in allrows if g in x['model_outputs'])) for g in GROUPS},
        'safe_boundary': {'automatic_mitigation_enabled': False, 'approved_for_execution': False, 'executed': False, 'effective_action': 'hold'},
        'independent_reviewer_proven': False,
        'prospective_healthy_false_alarm_rate': None,
        'step4_heldout_healthy_false_alarm_rate': None,
        'status': 'SHADOW_OBSERVATIONS_CAPTURED_NOT_FULLY_VALIDATED',
        'limitations': ['No autonomous mitigation', 'No independent review without original feature snapshots',
                        'Prospective healthy labels and adequate observation duration not established',
                        'Replayed or duplicate snapshots are not new independent observations']
    }
    if any(x['model_outputs'][g]['status'] == 'error' for x in entries for g in GROUPS):
        summary['status'] = 'SHADOW_INFERENCE_ERRORS'
    (OUT/'STEP5_REPORT.json').write_text(json.dumps(summary, indent=2)+'\n', encoding='utf-8')
    (OUT/'STEP5_REPORT.md').write_text('\n'.join([
        '# AdaptiveNet Step 5 — shadow-only observations', '', f"Status: **{summary['status']}**",
        f"Samples logged: {summary['total_logged_samples']}; distinct feature snapshots: {summary['distinct_snapshot_ids']}.",
        *[f"- {g}: {summary['by_group'][g]}" for g in GROUPS], '',
        '**No traffic control, automatic mitigation, or independent reviewer approval was performed.**',
        'Step 4 false alarms remain not estimable. Shadow logs alone are not ground-truth validation.',
        'Do not claim production readiness until real prospectively labeled healthy/degraded observations and reviewer integration are verified.', ''
    ]), encoding='utf-8')
    print('STEP 5 OBSERVATIONS SAVED:', OUT)
    print('STEP 5 STATUS:',summary['status'])
    return 2 if summary['status'] == 'SHADOW_INFERENCE_ERRORS' else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f'STEP 5 FAILED SAFELY: {type(exc).__name__}: {exc}', file=sys.stderr)
        raise SystemExit(2)
