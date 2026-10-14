#!/usr/bin/env python3
"""Read-only status of configured CloudFront distribution and CloudWatch telemetry."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE/'services'))
from adaptivenet.ingestion.cloudfront import distribution_id,collect_cloudfront,STANDARD

def main():
    did=distribution_id(str(BASE/'.env'))
    if did is None:
        print("NOT CONFIGURED: Set CLOUDFRONT_DISTRIBUTION_ID in local .env after Terraform deployment.")
        return 0
    profile=os.environ.get('AWS_PROFILE','adaptivenet-admin')
    cmd=['aws','cloudfront','get-distribution','--id',did,'--profile',profile,
         '--region','us-east-1','--query','Distribution.{Status:Status,Enabled:DistributionConfig.Enabled,Domain:DomainName}',
         '--output','json','--no-cli-pager']
    p=subprocess.run(cmd,text=True,capture_output=True,check=False,timeout=25)
    if p.returncode:
        print('ERROR: read-only CloudFront status query failed; check profile and cloudfront:GetDistribution permission.')
        return 2
    print('Distribution:',p.stdout.strip())
    try:
        items=collect_cloudfront(did,profile)
    except (OSError,RuntimeError,ValueError,TimeoutError) as exc:
        print('ERROR:',type(exc).__name__,'during CloudWatch read; need cloudwatch:GetMetricData us-east-1')
        return 2
    values={i.metric:i.value for i in items}
    print(json.dumps({'cloudwatch_region':'us-east-1','distribution_id':did,
                      'fresh_standard_metrics':values,
                      'missing': [name for name in STANDARD if name not in values]},indent=2))
    return 0

if __name__=='__main__':raise SystemExit(main())
