#!/usr/bin/env python3
"""Read-only local HTTP smoke of actual fresh archived telemetry, 3 frozen models and reviewer.

Patch: account for Python/scikit-learn cold start; do not hide server errors; disable
system HTTP proxies for loopback; fetch a fresh snapshot after readiness.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
from urllib import request, error

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
OUT=ROOT/'data/ml-results/native/step7-shadow-handoff'
ARCHIVE=ROOT/'data/ml-archive/feature_snapshots.jsonl'
WITNESS=ROOT/'data/ml-archive/http_witness.jsonl'
# Windows imports of sklearn/scipy and first 3-model inference may take longer
# than the original 8-second read timeout; never modify source freshness gates.
STARTUP_BUDGET_SECONDS=40
INFERENCE_TIMEOUT_SECONDS=60


def call(url, *, body=None, token=None, timeout=8):
    data=json.dumps(body).encode('utf-8') if body is not None else None
    headers={'Content-Type':'application/json'}
    if token:headers['Authorization']='Bearer '+token
    req=request.Request(url,data=data,headers=headers,method='POST' if body is not None else 'GET')
    # A local 127.0.0.1 test must not be routed through corporate/system proxies.
    opener=request.build_opener(request.ProxyHandler({}))
    try:
        with opener.open(req,timeout=timeout) as resp:
            return resp.status,json.loads(resp.read(2_000_000))
    except error.HTTPError as exc:
        return exc.code,json.loads(exc.read(2_000_000))


def main():
    from step5_finish.finish_step5_native import load_recent_snapshots,load_http_asof
    from step7_finish.finish_step7 import audit_models
    audit_models()
    token=secrets.token_urlsafe(48)
    with socket.socket() as s:
        s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    env=os.environ.copy()
    env.update({'ADAPTIVENET_SHADOW_TOKEN':token,'ADAPTIVENET_SHADOW_BIND':'127.0.0.1',
                'ADAPTIVENET_SHADOW_PORT':str(port),'PYTHONDONTWRITEBYTECODE':'1'})
    OUT.mkdir(parents=True,exist_ok=True)
    log_path=OUT/'STEP7_LOCAL_SERVICE_DIAGNOSTICS.log'
    with log_path.open('w',encoding='utf-8') as service_log:
        proc=subprocess.Popen([sys.executable,'-m','step7_finish.service'],cwd=ROOT,env=env,
                              stdout=service_log,stderr=subprocess.STDOUT)
        url=f'http://127.0.0.1:{port}'
        try:
            deadline=time.monotonic()+STARTUP_BUDGET_SECONDS
            while True:
                if proc.poll() is not None:
                    raise RuntimeError(f'Local service exited during startup (code {proc.returncode}); see {log_path}')
                try:
                    status,_=call(url+'/readyz',timeout=2)
                    if status==200:break
                except (OSError,TimeoutError):
                    pass
                if time.monotonic()>=deadline:
                    raise RuntimeError(f'Local service not ready within {STARTUP_BUDGET_SECONDS}s; see {log_path}')
                time.sleep(.2)
            # Fresh data is chosen only after startup, preserving the unmodified 180s gate.
            snap=load_recent_snapshots(ARCHIVE,1,time.time())[-1]
            events=load_http_asof(WITNESS,snap['computed_at_unix'])
            if not events:
                raise ValueError('No genuine as-of HTTP witnesses; existing collector must be running')
            body={'original_snapshot':snap,'http_witness_events':events}
            status,_=call(url+'/v1/infer',body=body,timeout=8)
            if status!=401:
                raise RuntimeError(f'Unauthorized inference was not rejected (HTTP {status})')
            print('STEP 7: service ready; authentication rejection passed; checking 3-model inference...',flush=True)
            try:
                status,result=call(url+'/v1/infer',body=body,token=token,timeout=INFERENCE_TIMEOUT_SECONDS)
            except (TimeoutError, OSError) as exc:
                raise RuntimeError(f'Local inference HTTP request failed ({type(exc).__name__}) after up to '
                                   f'{INFERENCE_TIMEOUT_SECONDS}s; see {log_path}') from exc
            if status!=200:
                raise RuntimeError(f'Authenticated inference failed HTTP {status}: '
                                   f'{result.get("error")}; {result.get("detail", "")}; see {log_path}')
            if result.get('snapshot_id') != snap['snapshot_id']:
                raise RuntimeError('Snapshot ID mismatch')
            models=result.get('models',{})
            if set(models)!={'aws_only','aws_ripe','aws_ripe_http'}:
                raise RuntimeError('Did not return all three model groups')
            if result.get('executed') is not False or result.get('approved_for_execution') is not False or result.get('effective_action')!='hold':
                raise RuntimeError('Actuation boundary violated')
            for group,entry in models.items():
                rev=entry.get('independent_review') or {}
                if rev.get('policy_valid') is not True or rev.get('executed') is not False or rev.get('effective_action')!='hold':
                    raise RuntimeError(f'{group} independent review failed')
            summary={'status':'STEP7_LOCAL_AUTHENTICATED_SERVICE_VERIFIED_SHADOW_ONLY',
                     'snapshot_id':snap['snapshot_id'],'snapshot_age_seconds':round(time.time()-snap['computed_at_unix'],1),
                     'http_witness_rows':len(events),'authentication_rejects_unauthorized':True,
                     'all_three_model_groups_returned':True,
                     'model_statuses':{g:v['status'] for g,v in models.items()},
                     'independent_reviews_valid':True,'no_execution':True,
                     'docker_image_build_verified':False,'eks_deployment_verified':False,
                     'notes':['Local authenticated HTTP smoke only; not proof of Kubernetes deployment',
                              'Source telemetry may contain mixed health; no healthy false-alarm claims']}
            (OUT/'STEP7_LOCAL_SMOKE.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8')
            print('STEP 7 LOCAL HTTP SMOKE:',summary['status'])
            print('MODEL OUTPUTS:',', '.join(f'{g}={v}' for g,v in summary['model_statuses'].items()))
            print('AUTHENTICATION:', 'unauthorized rejected; token-authenticated success')
            print('INDEPENDENT REVIEW VALID: True | SAFE HOLD ONLY: True')
            print('SAVED:',OUT/'STEP7_LOCAL_SMOKE.json')
            return 0
        finally:
            if proc.poll() is None:
                proc.terminate()
            try:proc.wait(timeout=3)
            except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=3)


if __name__=='__main__':
    try:sys.exit(main())
    except Exception as exc:
        print(f'STEP 7 LOCAL SMOKE NOT COMPLETE: {type(exc).__name__}: {exc}',file=sys.stderr)
        sys.exit(2)
