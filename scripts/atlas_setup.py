#!/usr/bin/env python3
"""Bounded, explicit RIPE Atlas ping measurement creation for AdaptiveNet.

Dry-run by default; uses only Python's standard library.
"""
import argparse
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API_URL = 'https://atlas.ripe.net/api/v2/measurements/'


def public_target(value):
    if not value or len(value) > 253 or any(c in value for c in '/:@?#\\ '):
        raise ValueError('Use a public IPv4 address or a DNS hostname without a scheme, path or port')
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        # No literal IPv6 for this IPv4-only ping configuration.
        if not re.fullmatch(r'(?=.{1,253}$)[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+', value):
            raise ValueError('Invalid DNS hostname')
        if not all(len(part) <= 63 and not part.startswith('-') and not part.endswith('-') for part in value.split('.')):
            raise ValueError('Invalid DNS hostname label')
    else:
        if ip.version != 4 or not ip.is_global:
            raise ValueError('Target must be a public IPv4 address')
    return value


def make_payload(sg, us, probes, interval, duration_hours, now=None):
    if not (1 <= probes <= 20):
        raise ValueError('probes must be between 1 and 20')
    if not (900 <= interval <= 3600):
        raise ValueError('interval must be between 900 and 3600 seconds')
    if not (1 <= duration_hours <= 24):
        raise ValueError('duration-hours must be between 1 and 24')
    if now is None:
        now = datetime.now(timezone.utc)
    stop = now + timedelta(hours=duration_hours)
    return {
        'is_oneoff': False,
        'stop_time': stop.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'definitions': [
            {'target': public_target(sg), 'description': 'AdaptiveNet Region A Singapore ping',
             'type': 'ping', 'af': 4, 'interval': interval},
            {'target': public_target(us), 'description': 'AdaptiveNet Region B Virginia ping',
             'type': 'ping', 'af': 4, 'interval': interval},
        ],
        'probes': [{'requested': probes, 'type': 'area', 'value': 'WW'}],
    }


def create(payload, key):
    req = Request(API_URL, data=json.dumps(payload).encode('utf-8'), method='POST',
                  headers={'Authorization': f'Key {key}', 'Content-Type': 'application/json',
                           'Accept': 'application/json', 'User-Agent': 'AdaptiveNet-Atlas-Setup/1.0'})
    try:
        with urlopen(req, timeout=30) as response:
            data = json.load(response)
    except HTTPError as exc:
        error_body = exc.read(1000).decode('utf-8', 'replace')
        raise RuntimeError(f'RIPE Atlas API HTTP {exc.code}: {error_body}') from exc
    except URLError as exc:
        raise RuntimeError(f'Could not contact RIPE Atlas: {exc.reason}') from exc
    ids = data.get('measurements')
    if not isinstance(ids, list) or len(ids) != 2 or not all(isinstance(i, int) for i in ids):
        raise RuntimeError(f'Unexpected RIPE Atlas response: {data!r}; inspect RIPE account before retrying')
    return ids


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--region-a', required=True, help='Singapore public IPv4 address or hostname')
    p.add_argument('--region-b', required=True, help='Virginia public IPv4 address or hostname')
    p.add_argument('--probes', type=int, default=5, help='Global probe count, default 5')
    p.add_argument('--interval', type=int, default=900, help='Seconds, default 900')
    p.add_argument('--duration-hours', type=int, default=6, help='Auto-stop in 1–24 hours, default 6')
    p.add_argument('--create', action='store_true', help='Create credit-consuming measurements; requires confirmation')
    args = p.parse_args(argv)
    try:
        payload = make_payload(args.region_a, args.region_b, args.probes, args.interval, args.duration_hours)
    except ValueError as exc:
        p.error(str(exc))
    print(json.dumps({'mode': 'CREATE' if args.create else 'DRY RUN — no RIPE API POST', 'payload': payload}, indent=2))
    if not args.create:
        print('\nNo RIPE credits used. Re-run with --create only after reviewing account credits and parameters.')
        return 0
    key = os.getenv('ATLAS_API_KEY', '').strip()
    if not key:
        p.error('Set ATLAS_API_KEY locally (do not paste it into chat or commit it to Git).')
    if not sys.stdin.isatty():
        p.error('Interactive confirmation required. No measurements created.')
    if input('Create TWO credit-consuming, recurring measurements that auto-stop? Type CREATE: ').strip() != 'CREATE':
        print('Cancelled. No measurements created.')
        return 1
    try:
        ids = create(payload, key)
    except RuntimeError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2
    print('\nCREATED. Save this in your local untracked .env:')
    print(f'ATLAS_MEASUREMENTS=region-a={ids[0]},region-b={ids[1]}')
    print('Auto-stop UTC:', payload['stop_time'])
    print('RIPE measurements:', *(f'https://atlas.ripe.net/measurements/{i}/' for i in ids), sep='\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
