#!/usr/bin/env python3
"""Send bounded real GETs through configured CloudFront domain, without changing AWS."""
import argparse
import json
import os
from pathlib import Path
import subprocess
from urllib.request import urlopen
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services'))
from adaptivenet.ingestion.cloudfront import distribution_id

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--count',type=int,default=5)
    p.add_argument('--run',action='store_true')
    p.add_argument('--profile',default='adaptivenet-admin')
    a=p.parse_args()
    if a.count<1 or a.count>20:p.error('count must be 1..20')
    did=distribution_id(str(ROOT/'.env'))
    if not did:p.error('CLOUDFRONT_DISTRIBUTION_ID must be set in .env')
    cmd=['aws','cloudfront','get-distribution','--id',did,'--profile',a.profile,'--region','us-east-1',
         '--query','Distribution.DomainName','--output','text','--no-cli-pager']
    result=subprocess.run(cmd,capture_output=True,text=True,timeout=25,check=False)
    if result.returncode:
        p.error('CloudFront distribution not accessible; check AWS profile and IAM read permission')
    domain=result.stdout.strip()
    if not domain.endswith('.cloudfront.net') or any(c in domain for c in '/:@ '):
        p.error('unexpected CloudFront domain')
    url='https://'+domain+'/api/catalog'
    print('Plan:',a.count,'GET requests through',url)
    if not a.run:
        print('DRY RUN. Supply --run to send requests.');return 0
    for i in range(a.count):
        with urlopen(url,timeout=15) as response:
            data=json.load(response)
            if response.status!=200 or data.get('region') not in ('region-a','region-b'):
                raise ValueError('unexpected response from CloudFront')
            print(f"Request {i+1}: HTTP {response.status} origin={data['region']}")
    return 0

if __name__=='__main__':raise SystemExit(main())
