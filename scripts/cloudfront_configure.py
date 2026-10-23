#!/usr/bin/env python3
"""Safely set a CloudFront ID in ignored .env (no AWS writes)."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services'))
from adaptivenet.ingestion.cloudfront import DISTRIBUTION_RE


def save_distribution(env: Path, distribution: str, backups: Path | None = None):
    if not DISTRIBUTION_RE.fullmatch(distribution):
        raise ValueError('invalid CloudFront distribution ID')
    source = env.read_text(encoding='utf-8') if env.exists() else ''
    rows = [line for line in source.splitlines()
            if not re.match(r'^\s*CLOUDFRONT_DISTRIBUTION_ID\s*=', line)]
    rows.append('CLOUDFRONT_DISTRIBUTION_ID=' + distribution)
    result='\n'.join(rows) + '\n'
    if result == source:
        return False
    env.parent.mkdir(parents=True, exist_ok=True)
    if env.exists():
        folder=backups or (ROOT / 'data' / 'config-backups')
        folder.mkdir(parents=True, exist_ok=True)
        path=folder / f'env-before-cloudfront-{time.time_ns()}.bak'
        shutil.copy2(env,path)
        try: os.chmod(path,0o600)
        except OSError: pass
    fd,tmp=tempfile.mkstemp(prefix='.env-cloudfront-',dir=str(env.parent))
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:f.write(result)
        try: os.chmod(tmp,(env.stat().st_mode & 0o777) if env.exists() else 0o600)
        except OSError: pass
        os.replace(tmp,env)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    return True


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--id',required=True,help='Deployed CloudFront distribution ID (starts with E)')
    p.add_argument('--env-file',default=str(ROOT/'.env'))
    a=p.parse_args()
    try: changed=save_distribution(Path(a.env_file),a.id)
    except (OSError,ValueError) as e: p.error(str(e))
    print('Saved ID to ignored .env.' if changed else 'Already configured.')
    if os.getenv('CLOUDFRONT_DISTRIBUTION_ID'):
        print('WARNING: Shell CLOUDFRONT_DISTRIBUTION_ID overrides the .env value.')
    print('Restart ingestion watch, run once and cloudfront_doctor.py.')

if __name__=='__main__':main()
