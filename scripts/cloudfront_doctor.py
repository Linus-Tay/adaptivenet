#!/usr/bin/env python3
"""Read-only CloudFront distribution and CloudWatch diagnostics, including older samples."""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import subprocess
import sys

BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE/'services'))
from adaptivenet.ingestion.cloudfront import (
    distribution_id, collect_cloudfront, STANDARD, metric_queries, FRESH_SECONDS
)


def history_diagnostics(distribution, profile, *, lookback_minutes=180, now=None):
    """Check historic real points without falsely passing them to live ingestion."""
    now=now or datetime.now(timezone.utc)
    cmd=['aws','cloudwatch','get-metric-data','--profile',profile,'--region','us-east-1',
         '--metric-data-queries',json.dumps(metric_queries(distribution)),
         '--start-time',(now-timedelta(minutes=lookback_minutes)).isoformat(),
         '--end-time',now.isoformat(),'--scan-by','TimestampDescending',
         '--output','json','--no-cli-pager']
    reply=subprocess.run(cmd,capture_output=True,text=True,timeout=40,check=False,
                         env={**os.environ,'AWS_PAGER':''})
    if reply.returncode:
        raise RuntimeError('cloudwatch_history_query_failed')
    try: body=json.loads(reply.stdout)
    except (ValueError,TypeError) as exc: raise ValueError('cloudwatch_history_invalid_json') from exc
    if not isinstance(body,dict) or not isinstance(body.get('MetricDataResults'),list):
        raise ValueError('cloudwatch_history_invalid_shape')
    names=list(STANDARD)
    reports={}
    for row in body['MetricDataResults']:
        if not isinstance(row,dict): continue
        key=row.get('Id')
        if not isinstance(key,str) or key not in {f'm{i}' for i in range(len(names))}:continue
        stamps,values=row.get('Timestamps',[]),row.get('Values',[])
        points=[]
        if isinstance(stamps,list) and isinstance(values,list) and len(stamps)==len(values):
            for stamp,value in zip(stamps,values):
                try:
                    dt=datetime.fromisoformat(stamp.replace('Z','+00:00'))
                    if dt.tzinfo is None or type(value) not in (float,int) or not math.isfinite(value):
                        continue
                    age=(now-dt).total_seconds()
                    if age< -60 or age>lookback_minutes*60+60:continue
                except (TypeError,ValueError,OverflowError):
                    continue
                points.append((dt.timestamp(),value,age))
        latest=max(points,default=None)
        reports[names[int(key[1:])]]={
            'cloudwatch_status':str(row.get('StatusCode','unknown'))[:32],
            'sample_count':len(points),
            'latest_value':latest[1] if latest else None,
            'last_sample_age_seconds':round(latest[2]) if latest else None,
            'within_live_freshness':latest is not None and 0<=latest[2]<=FRESH_SECONDS
        }
    for name in names:
        reports.setdefault(name,{'cloudwatch_status':'not_returned','sample_count':0,
                                'latest_value':None,'last_sample_age_seconds':None,
                                'within_live_freshness':False})
    return {'lookback_minutes':lookback_minutes,'metric_samples':reports}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',default=os.environ.get('AWS_PROFILE','adaptivenet-admin'))
    parser.add_argument('--lookback-minutes',type=int,default=180)
    args=parser.parse_args(argv)
    if not 20<=args.lookback_minutes<=360:
        parser.error('--lookback-minutes must be between 20 and 360')
    try: did=distribution_id(str(BASE/'.env'))
    except ValueError:
        print('Invalid CloudFront distribution ID; check .env and shell overrides.')
        return 2
    if not did:
        print('NOT CONFIGURED: Set CLOUDFRONT_DISTRIBUTION_ID in local .env.')
        return 2
    cmd=['aws','cloudfront','get-distribution','--id',did,'--profile',args.profile,
         '--region','us-east-1','--query',
         'Distribution.{Status:Status,Enabled:DistributionConfig.Enabled,Domain:DomainName}',
         '--output','json','--no-cli-pager']
    try:
        reply=subprocess.run(cmd,capture_output=True,text=True,check=False,timeout=25)
        if reply.returncode:
            print('ERROR: CloudFront distribution read failed; check SSO and GetDistribution access.')
            return 2
        print('Distribution:',reply.stdout.strip())
        recent=collect_cloudfront(did,args.profile)
        history=history_diagnostics(did,args.profile,lookback_minutes=args.lookback_minutes)
    except (OSError,RuntimeError,ValueError,TimeoutError) as exc:
        print('ERROR:',type(exc).__name__,'while reading CloudWatch; verify GetMetricData in us-east-1.')
        return 2
    values={item.metric:item.value for item in recent}
    missing=[name for name in STANDARD if name not in values]
    if not missing:
        hint='Fresh metrics available. Run ingestion once, then refresh dashboard.'
    elif any(history['metric_samples'][name]['sample_count'] for name in STANDARD):
        hint='Historical points exist, but some are too old or missing for the 15-minute live gate. Send new requests, wait for publication, then collect again.'
    else:
        hint='No CloudWatch points in the extended window. Check successful CloudFront HTTPS requests, distribution ID and CloudWatch Region=Global in us-east-1.'
    print(json.dumps({'distribution_id':did,'cloudwatch_region':'us-east-1',
                      'fresh_standard_metrics':values,'missing':missing,
                      'historical_diagnostic':history,'next_step':hint},indent=2))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
