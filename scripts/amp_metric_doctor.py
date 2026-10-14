#!/usr/bin/env python3
"""Read-only AMP metric-discovery diagnostics, no mutation, no secrets printed.

The app's /api/catalog counters are lazy: first request creates the series,
then 2+ Prometheus scrapes are needed before rate() produces observations.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlencode

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'services'))
from adaptivenet.ingestion.config import region_configs
from adaptivenet.ingestion.sources import PROMQL, amp_error_code

SAFE_LABELS=('project','job','region','route','status')


def query(cfg, expr: str, profile: str, *, timeout=25) -> dict:
    endpoint=(f'https://aps-workspaces.{cfg.aws_region}.amazonaws.com/workspaces/'
              f'{cfg.workspace}/api/v1/query')
    cmd=['awscurl','--region',cfg.aws_region,'--service','aps','-X','POST',endpoint,
         '--header','Content-Type: application/x-www-form-urlencoded',
         '-d',urlencode({'query':expr})]
    try:
        result=subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
            check=False,env={**os.environ,'AWS_PROFILE':profile,'AWS_PAGER':''})
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {'state':'client_'+type(exc).__name__}
    if result.returncode != 0:
        return {'state':'signed_request_failed','exit_code':result.returncode}
    try:
        payload=json.loads(result.stdout)
    except (ValueError,TypeError):
        return {'state':'invalid_json'}
    if not isinstance(payload,dict) or payload.get('status')!='success':
        return {'state':amp_error_code(payload)}
    data=payload.get('data',{})
    if not isinstance(data,dict) or data.get('resultType')!='vector':
        return {'state':'unexpected_response_type'}
    matches=data.get('result',[])
    if not isinstance(matches,list):
        return {'state':'invalid_vector'}
    report={'state':'present' if matches else 'no_series','series_count':len(matches)}
    if matches:
        first=matches[0]
        labels=first.get('metric',{})
        if isinstance(labels,dict):
            report['first_series_labels']={key:str(labels[key]) for key in SAFE_LABELS if key in labels}
        pair=first.get('value')
        if isinstance(pair,list) and len(pair)==2:
            try:
                value=float(pair[1])
                if abs(value)<1e15:
                    report['first_value']=value
            except (TypeError,ValueError):
                pass
    return report


def diagnose(cfg, profile: str, timeout=25) -> dict:
    name=cfg.name
    tests={
        'app_up':PROMQL['app_up'][0].replace('{name}',name),
        'memory_available':PROMQL['memory_available_bytes'][0].replace('{name}',name),
        'memory_total':PROMQL['memory_total_bytes'][0].replace('{name}',name),
        'catalog_raw':'adaptivenet_http_requests_total',
        'catalog_route': 'adaptivenet_http_requests_total{route="/api/catalog"}',
        'catalog_filtered': f'adaptivenet_http_requests_total{{route="/api/catalog",region="{name}",project="adaptivenet"}}',
        'catalog_scrapes_5m':f'count_over_time(adaptivenet_http_requests_total{{route="/api/catalog",region="{name}",project="adaptivenet"}}[5m])',
        'http_rate_5m':PROMQL['http_rps'][0].replace('{name}',name),
        'histogram_raw':'adaptivenet_http_request_duration_seconds_bucket',
        'histogram_filtered':f'adaptivenet_http_request_duration_seconds_bucket{{route="/api/catalog",region="{name}",project="adaptivenet"}}',
        'http_p95_5m':PROMQL['http_p95_ms'][0].replace('{name}',name),
    }
    outcomes={key:query(cfg,expression,profile,timeout=timeout) for key,expression in tests.items()}
    if outcomes['app_up']['state']=='no_series' or outcomes['memory_available']['state']=='no_series' or outcomes['memory_total']['state']=='no_series':
        hint='A required internal metric is missing; inspect app_up and memory sample states before relying on region health.'
    elif outcomes['catalog_raw']['state']=='no_series':
        hint='No catalog counters in AMP yet. Check /metrics on the EC2 instance and remote-write scraping.'
    elif outcomes['catalog_route']['state']=='no_series':
        hint='Counters exist but not for /api/catalog; verify exporter route label and invoke catalog probe.'
    elif outcomes['catalog_filtered']['state']=='no_series':
        hint='Route counters exist but filtered region/project labels do not match. Inspect first_series_labels.'
    elif outcomes['http_rate_5m']['state']=='no_series':
        sample_count=outcomes['catalog_scrapes_5m'].get('first_value')
        if sample_count is not None and sample_count < 2:
            hint='Only one or fewer recent samples in the 5m range; rate() requires 2+ scrapes. Wait, then rerun.'
        else:
            hint='Catalog series exists but rate() returned no data. Inspect 5m scrape counts and series labels; do not assume zero.'
    elif outcomes['histogram_raw']['state']=='no_series':
        hint='Request counters exist but histograms absent; inspect deployed /metrics exporter.'
    elif outcomes['http_p95_5m']['state']=='no_series':
        hint='Histogram present, but p95 rate window unavailable; collect multiple scrapes and verify le labels.'
    elif outcomes['http_rate_5m']['state']=='present':
        hint='HTTP rates present in AMP; run ingestion once and inspect feature provenance.'
    else:
        hint='Review the per-query diagnostic states.'
    return {'region':cfg.name,'aws_region':cfg.aws_region,'checks':outcomes,'diagnosis':hint}


def main(argv=None):
    parser=argparse.ArgumentParser(description='Read-only live AWS AMP metric diagnosis')
    parser.add_argument('--profile',default='adaptivenet-admin')
    parser.add_argument('--env-file',default='.env')
    args=parser.parse_args(argv)
    print('READ ONLY: queries AMP only; no EC2/RIPE/DB/AWS mutations', flush=True)
    for cfg in region_configs(args.env_file):
        print(json.dumps(diagnose(cfg,args.profile),indent=2),flush=True)
    return 0

if __name__=='__main__':
    raise SystemExit(main())
