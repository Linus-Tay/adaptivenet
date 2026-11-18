#!/usr/bin/env python3
"""Copy the three frozen network models into data/demo-models/ for the demo's shadow ML panel.

  python scripts/export_demo_models.py --root /path/to/folder/holding/data

--root is the folder whose data/ml-results/native/ holds the Step 3 models and the
Step 4 report (default: this repository). Each model's SHA-256 is checked against the
frozen Step 4 report before it is copied, so only the evaluated artifacts are used.
The copies are named aws_only.pkl, aws_ripe.pkl and aws_ripe_http.pkl. data/ is git-ignored.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from step7_finish.finish_step7 import audit_models  # noqa: E402


def export(source_root: Path, dest: Path) -> dict:
    models = audit_models(source_root)
    dest.mkdir(parents=True, exist_ok=True)
    for group, m in models.items():
        shutil.copyfile(source_root / "data/ml-results/native/step3-network-models" / m["filename"], dest / f"{group}.pkl")
    return models


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "demo-models")
    a = ap.parse_args(argv)
    for group, m in export(a.root.resolve(), a.out).items():
        print(f"{group}.pkl <- {m['model_id']} (sha256 {m['sha256'][:12]}..., verified)")
    print(f"Copied to {a.out}. Restart the demo (demo/run.sh lab-up) to load them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
