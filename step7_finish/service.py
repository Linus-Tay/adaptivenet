#!/usr/bin/env python3
"""AdaptiveNet shadow-only inference HTTP service.

Only trusted internal clients with a shared token may submit original evidence.
There are no cloud clients or actuator calls in this service.
"""
from __future__ import annotations

import hmac
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
GROUPS = ('aws_only', 'aws_ripe', 'aws_ripe_http')
MAX_BODY = 2 * 1024 * 1024


def require_valid_snapshot(snapshot: dict, *, now: float | None = None) -> tuple[str, float]:
    """Refuse stale/invalid/mismatched original snapshots, never alter timestamps."""
    if not isinstance(snapshot, dict) or snapshot.get('contract_version') != 'adaptivenet.features.v1':
        raise ValueError('original adaptivenet.features.v1 snapshot required')
    ident = snapshot.get('snapshot_id')
    stamp = snapshot.get('computed_at_unix')
    if not isinstance(ident, str) or not ident.startswith('features-'):
        raise ValueError('invalid original snapshot ID')
    if isinstance(stamp, bool) or not isinstance(stamp, (int,float)) or not math.isfinite(stamp):
        raise ValueError('invalid original timestamp')
    age = (time.time() if now is None else now) - stamp
    if not 0 <= age <= 180:
        raise ValueError('snapshot stale or future dated (>180s)')
    if snapshot.get('ready_for_model') is not True or snapshot.get('source_errors') or snapshot.get('quality_reasons'):
        raise ValueError('original source not ready or has quality errors')
    if snapshot.get('ready_for_automatic_action') is True:
        raise ValueError('automatic action source flag is not allowed')
    return ident, float(stamp)


def verify_witnesses(events: list, cutoff: float) -> None:
    if not isinstance(events, list) or len(events)>5000:
        raise ValueError('HTTP witness list required (max 5000)')
    from ml.team_starter.verified_labels import unix
    for idx, e in enumerate(events):
        if not isinstance(e, dict) or e.get('scope') not in ('region-a','region-b','cloudfront'):
            raise ValueError(f'Invalid HTTP witness scope/index {idx}')
        stamp = unix(e.get('timestamp'))
        if stamp > cutoff:
            raise ValueError('future HTTP witness rejected; no feature leakage')
        if cutoff - stamp > 900:
            raise ValueError('HTTP witness outside 900s window')


def evaluate(request: dict, paths: dict, *, predict=None, review=None, make_slow=None, make_fast=None, now=None) -> dict:
    if not isinstance(request, dict) or set(request) != {'original_snapshot','http_witness_events'}:
        raise ValueError('requires only original_snapshot and http_witness_events')
    original = request['original_snapshot']
    ident, stamp = require_valid_snapshot(original, now=now)
    events = request['http_witness_events']
    verify_witnesses(events,stamp)
    if predict is None:
        from ml.team_starter.shadow import shadow_predict as predict
    if review is None:
        from ml.team_starter.shadow import shadow_review as review
    if make_slow is None:
        from adaptivenet.ingestion.handoff import build_model_input as make_slow
    if make_fast is None:
        from adaptivenet.ingestion.fast_signals import build_fast_signals as make_fast
    # Features reconstructed from the original; client cannot supply edited model data.
    slow = make_slow(original, mode='combined', now=stamp)
    if slow.get('contract_version') != 'adaptivenet.model_input.v1' or slow.get('snapshot_id') != ident or slow.get('computed_at_unix') != stamp:
        raise ValueError('upstream model conversion changed original identity/timestamp')
    if slow.get('actions_enabled') is not False:
        raise ValueError('upstream converter failed to disable actions')
    fast = make_fast(events, now=stamp)
    if fast.get('contract_version') != 'adaptivenet.fast_signals.v1':
        raise ValueError('invalid fast feature contract')
    detection = {'contract_version':'adaptivenet.detection_input.v1','slow':slow,'fast':fast}
    outcomes = {}
    for group in GROUPS:
        proposal = predict(detection, paths[group])
        if not isinstance(proposal, dict) or proposal.get('contract_version') != 'adaptivenet.proposal.v1' or proposal.get('snapshot_id') != ident:
            raise ValueError(f'{group} invalid proposal contract/identity')
        if proposal.get('action') not in ('hold','investigate'):
            raise ValueError(f'{group} unsafe model action')
        conf = proposal.get('confidence')
        if isinstance(conf, bool) or not isinstance(conf,(float,int)) or not math.isfinite(conf) or not 0<=conf<=1:
            raise ValueError(f'{group} invalid confidence')
        decision = review(original,proposal)  # team's independent safety reviewer
        if not isinstance(decision,dict) or decision.get('review_version') != 'adaptivenet.decision_review.v1' or decision.get('policy_valid') is not True:
            raise ValueError(f'{group} review invalid')
        if decision.get('approved_for_execution') is not False or decision.get('executed') is not False or decision.get('effective_action') != 'hold':
            raise ValueError(f'{group} review attempted execution')
        evidence=proposal.get('evidence',[])
        if not isinstance(evidence,list) or not all(isinstance(v,str) for v in evidence):
            raise ValueError(f'{group} invalid evidence')
        condition=next((v.split(':',1)[1] for v in evidence if v.startswith('shadow_condition:')),None)
        outcomes[group]={'status':'trained_prediction' if condition else 'abstained','condition':condition,
                         'confidence':float(conf),'proposal':proposal,
                         'independent_review':{'review_version':decision['review_version'],
                                               'policy_valid':True,'approved_for_execution':False,
                                               'executed':False,'effective_action':'hold'}}
    return {'contract_version':'adaptivenet.shadow_inference_response.v1',
            'snapshot_id':ident,'evaluated_at_utc':datetime.now(timezone.utc).isoformat(),
            'models':outcomes,'shadow_only':True,'automatic_mitigation_enabled':False,
            'approved_for_execution':False,'executed':False,'effective_action':'hold'}


def authorized(header: str | None, expected: str) -> bool:
    if not expected or not header or not header.startswith('Bearer '):
        return False
    return hmac.compare_digest(header[7:].encode('utf-8'),expected.encode('utf-8'))


def make_handler(paths, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Do not expose bearer tokens, request bodies or feature values.
            sys.stderr.write('[shadow-http] %s %s\n' % (self.client_address[0], str(args[0]) if args else 'event'))
        def send_json(self, code, obj):
            payload=json.dumps(obj,allow_nan=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(payload)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.end_headers()
            self.wfile.write(payload)
        def do_GET(self):
            if self.path=='/healthz':
                return self.send_json(200,{'status':'alive','shadow_only':True})
            if self.path=='/readyz':
                return self.send_json(200,{'status':'ready','model_artifact_hashes_verified_at_start':True,'shadow_only':True})
            self.send_json(404,{'error':'not_found'})
        def do_POST(self):
            if self.path != '/v1/infer':
                return self.send_json(404,{'error':'not_found'})
            if not authorized(self.headers.get('Authorization'),token):
                return self.send_json(401,{'error':'unauthorized'})
            try:
                length=int(self.headers.get('Content-Length','-1'))
            except (TypeError,ValueError):
                length=-1
            if not 0<length<=MAX_BODY:
                return self.send_json(413,{'error':'body_limit_or_content_length'})
            if self.headers.get('Content-Type','').split(';')[0].strip().lower() != 'application/json':
                return self.send_json(415,{'error':'json_required'})
            try:
                body=json.loads(self.rfile.read(length))
                result=evaluate(body,paths)
                self.send_json(200,result)
            except (ValueError,KeyError,TypeError) as exc:
                self.send_json(422,{'error':'invalid_or_unready_input','detail':str(exc)[:180]})
            except Exception as exc:
                # Fail closed and log only exception type; avoid exposing private data.
                sys.stderr.write('[shadow-http] inference error: %s\n' % type(exc).__name__)
                self.send_json(503,{'error':'shadow_inference_unavailable','approved_for_execution':False,'executed':False,'effective_action':'hold'})
    return Handler


def main():
    from step5_finish.finalize_step5 import preflight, read_json, STEP4
    token = os.environ.get('ADAPTIVENET_SHADOW_TOKEN','')
    if len(token)<24:
        raise SystemExit('ADAPTIVENET_SHADOW_TOKEN missing/too short; refusing startup')
    paths=preflight(read_json(STEP4))
    port=int(os.environ.get('ADAPTIVENET_SHADOW_PORT','8090'))
    if not 1<=port<=65535:
        raise SystemExit('invalid port')
    host=os.environ.get('ADAPTIVENET_SHADOW_BIND','127.0.0.1')
    if host not in ('127.0.0.1','0.0.0.0'):
        raise SystemExit('binding must be local or container internal')
    print('AdaptiveNet shadow inference listening; 3 frozen models verified; mitigation disabled',flush=True)
    ThreadingHTTPServer((host,port),make_handler(paths,token)).serve_forever()


if __name__=='__main__':
    main()
