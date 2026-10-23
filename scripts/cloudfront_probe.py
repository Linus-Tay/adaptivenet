#!/usr/bin/env python3
"""Send bounded real GETs through configured CloudFront domain, without changing AWS."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import ssl
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services'))
from adaptivenet.ingestion.cloudfront import distribution_id
from adaptivenet.ingestion.sources import atlas_tls_context, UpstreamDataError


def verified_tls_context():
    """Use configured/system trust plus certifi, without bypassing TLS checks.

    python.org macOS Python can lack a system CA bundle until its optional
    Install Certificates.command is run. certifi is installed in the live
    collector quick-start, but remains optional for offline unit tests.
    """
    context = atlas_tls_context()
    try:
        import certifi
    except ImportError:
        pass
    else:
        context.load_verify_locations(cafile=certifi.where())
    return context

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
    try:
        context = verified_tls_context()
    except (OSError, ssl.SSLError, UpstreamDataError) as exc:
        print(f"Unable to load a verified HTTPS trust bundle: {type(exc).__name__}.", file=sys.stderr)
        return 2
    for i in range(a.count):
        try:
            with urlopen(url, timeout=15, context=context) as response:
                data = json.load(response)
                if response.status != 200 or data.get('region') not in ('region-a', 'region-b'):
                    raise ValueError('unexpected response from CloudFront')
                print(f"Request {i+1}: HTTP {response.status} origin={data['region']}")
        except HTTPError as exc:
            print(f"CloudFront returned HTTP {exc.code}; inspect /healthz and regional origins.", file=sys.stderr)
            return 2
        except URLError as exc:
            if isinstance(exc.reason, ssl.SSLCertVerificationError):
                print("HTTPS CA verification failed. Install/upgrade certifi and set SSL_CERT_FILE="
                      "$(python -c 'import certifi; print(certifi.where())') on macOS; "
                      "never disable certificate verification.", file=sys.stderr)
            else:
                print(f"CloudFront request failed: {type(exc.reason).__name__}.", file=sys.stderr)
            return 2
    return 0

if __name__=='__main__':raise SystemExit(main())
