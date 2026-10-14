#!/usr/bin/env python3
"""Run from the existing AdaptiveNet project root without reinstalling app."""
from pathlib import Path
import sys
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0,str(PROJECT/"services"))
from adaptivenet.ingestion.cli import main
if __name__ == "__main__":
    raise SystemExit(main())
