#!/usr/bin/env python3
"""Read-only bounded upstream diagnostics for the AdaptiveNet ingestion pipeline.

Never prints AWS credentials, authorization headers, API keys, full response bodies,
or arbitrary server error text. Does not write to the ingestion database.
"""
from pathlib import Path
import argparse
import json
import os
import ssl
import subprocess
import sys
import time
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE/'services'))
from adaptivenet.ingestion.config import region_configs
from adaptivenet.ingestion.sources import PROMQL, parse_prometheus, parse_atlas_latest, UpstreamDataError, safe_token, atlas_tls_context, amp_error_code


def amp(cfg, profile, metric, now):
    template, _ = PROMQL[metric]
    expr = template.replace('{name}',cfg.name)
    uri = (f'https://aps-workspaces.{cfg.aws_region}.amazonaws.com/workspaces/'
           f'{cfg.workspace}/api/v1/query')
    cmd=['awscurl','--region',cfg.aws_region,'--service','aps','-X','POST',uri,
         '--header','Content-Type: application/x-www-form-urlencoded',
         '-d',urlencode({'query':expr})]
    try:
        resp=subprocess.run(cmd,text=True,capture_output=True,timeout=24,check=False,env={**os.environ,'AWS_PROFILE':profile,'AWS_PAGER':''})
        if resp.returncode:
            return {'status':'command_failed','exit_code':resp.returncode,'stderr_type':'not_shown'}
        try: payload=json.loads(resp.stdout)
        except ValueError: return {'status':'non_json_response','bytes':len(resp.stdout)}
        data=payload.get('data',{}) if isinstance(payload,dict) else {}
        info={'status':safe_token(payload.get('status','absent')) if isinstance(payload,dict) else 'unexpected_type',
              'result_type':data.get('resultType') if isinstance(data,dict) else None,
              'error_type':safe_token(payload.get('errorType') or payload.get('__type') or payload.get('code') or payload.get('Code')) if isinstance(payload,dict) else None,
              'top_level_keys':sorted([k for k in payload.keys() if k in ('status','data','errorType','__type','code','Code','message','Message','error')]) if isinstance(payload,dict) else []}
        if info['status'] != 'success':
            info['diagnostic_code']=amp_error_code(payload)
        if info['status']=='success':
            try: result=parse_prometheus(payload,now)
            except UpstreamDataError as exc:
                info['normalized']='failed:'+exc.code
            except ValueError:
                info['normalized']='failed:ValueError'
            else:
                info['normalized']='no_recent_series' if result is None else 'valid'
                if result is not None:
                    info['age_seconds']=round(now-result[0],2)
                    if metric=='app_up':info['value']=result[1]
            raw=data.get('result',[]) if isinstance(data,dict) else []
            if isinstance(raw,list):
                info['series_count']=len(raw)
                if raw and isinstance(raw[0],dict) and 'value' in raw[0]:
                    v=raw[0]['value']
                    if isinstance(v,list) and v:
                        try:info['first_sample_age_seconds']=round(now-float(v[0]),2)
                        except (TypeError, ValueError):pass
        return info
    except subprocess.TimeoutExpired: return {'status':'command_timeout'}
    except OSError as exc: return {'status':'os_error','error_type':type(exc).__name__}


def atlas(cfg, now):
    uri=f'https://atlas.ripe.net/api/v2/measurements/{cfg.measurement}/latest/?versions=1'
    try:
        with urlopen(Request(uri,headers={'Accept':'application/json','User-Agent':'AdaptiveNet-Doctor/1.0'}),timeout=15,context=atlas_tls_context()) as res:
            raw=json.load(res)
        info={'status':'ok','shape':type(raw).__name__}
        if isinstance(raw,(dict,list)):
            info['raw_entries']=len(raw)
            try:
                events=parse_atlas_latest(raw,cfg,now)
                info['parsed_observations']=len(events)
                info['distinct_probes']=len({e.source_id for e in events})
                if events:
                    ages=[now-e.observed_at for e in events]
                    info['newest_age_seconds']=round(min(ages))
                    info['oldest_age_seconds']=round(max(ages))
            except UpstreamDataError as exc: info['normalization_error']=exc.code
            except ValueError:info['normalization_error']='ValueError'
        return info
    except UpstreamDataError as exc:return {'status':'trust_configuration_error','reason_type':exc.code}
    except HTTPError as exc:return {'status':'http_error','http_code':exc.code}
    except URLError as exc:return {'status':'connection_error','reason_type':type(exc.reason).__name__}
    except (TimeoutError, ssl.SSLError) as exc:return {'status':'connection_error','reason_type':type(exc).__name__}
    except ValueError:return {'status':'invalid_json_or_payload'}


def main():
    p=argparse.ArgumentParser(description='Read-only diagnostic: AWS Prometheus and RIPE Atlas')
    p.add_argument('--profile',default='adaptivenet-admin')
    p.add_argument('--env-file',default='.env')
    args=p.parse_args()
    now=time.time()
    print('Diagnostic mode: READ ONLY; no AWS writes or RIPE measurements created',flush=True)
    print('Local Unix time:',round(now),'SSL_CERT_FILE configured:',bool(os.getenv('SSL_CERT_FILE')),flush=True)
    try:regions=region_configs(args.env_file)
    except ValueError as exc:
        print('CONFIGURATION ERROR:',type(exc).__name__,'(check .env IDs)');return 2
    for cfg in regions:
        print('\n',cfg.name,cfg.aws_region,flush=True)
        for key in ('app_up','memory_available_bytes'):
            print('  AMP',key,json.dumps(amp(cfg,args.profile,key,now),sort_keys=True),flush=True)
        print('  ATLAS',json.dumps(atlas(cfg,now),sort_keys=True),flush=True)
    return 0

if __name__=='__main__':raise SystemExit(main())
