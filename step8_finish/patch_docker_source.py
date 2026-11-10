#!/usr/bin/env python3
"""Idempotently correct only Step 7 source Dockerfile's reserved Linux group name.

Prevents future regenerated build contexts from reintroducing the Docker failure.
No Docker, model, AWS or data operations.
"""
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parent.parent
SOURCE=ROOT/'step7_finish/Dockerfile'
OLD='groupadd -g 10001 shadow && useradd -M -u 10001 -g shadow shadow'
NEW='groupadd -g 10001 mlshadow && useradd -M -u 10001 -g mlshadow mlshadow'

def patch(path=SOURCE):
    if not path.is_file():raise FileNotFoundError('Step 7 source Dockerfile missing')
    content=path.read_text(encoding='utf-8')
    if NEW in content:
        print('STEP 7 SOURCE DOCKERFILE: already fixed')
        return False
    if content.count(OLD)!=1:
        raise ValueError('Dockerfile unexpected format: refusing to overwrite unrelated changes')
    path.write_text(content.replace(OLD,NEW),encoding='utf-8')
    print('STEP 7 SOURCE DOCKERFILE: reserved-group conflict fixed (idempotent)')
    return True

if __name__=='__main__':
    try:patch()
    except Exception as exc:
        print('FAIL-CLOSED:',type(exc).__name__+': '+str(exc),file=sys.stderr)
        raise SystemExit(2)
