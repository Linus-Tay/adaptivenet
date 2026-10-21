#!/usr/bin/env python3
"""Generate FAKE practice tick data so you can learn the training pipeline.

This data is simulated, not measured. Never report results from it in the
article. Replace data/ticks.jsonl with real lab ticks before evaluating.

Usage:
    python make_practice_ticks.py --out data/ticks.jsonl
"""
import argparse
import json
import random
from pathlib import Path

# (label, fault kind, training magnitudes, held-out magnitudes)
SCENARIOS = [
    ("healthy", "none", [0], [0]),
    ("network", "path_delay_ms", [1500], [900, 3000]),
    ("network", "path_loss", [0.6], [0.3, 0.85]),
    ("compute", "service_delay_ms", [1500], [900, 3000]),
    ("critical", "both_errors", [1.0], [1.0]),
]
PROBES = 5  # five HTTP probes per view per tick, as in the article


def view(extra_ms=0.0, error_rate=0.0):
    """One view's summary for one tick: p95 latency and error ratio of 5 probes."""
    times = [max(1.0, random.gauss(25, 8)) + extra_ms * random.uniform(0.95, 1.05)
             for _ in range(PROBES)]
    errors = sum(random.random() < error_rate for _ in range(PROBES))
    return round(max(times), 1), errors / PROBES


def run(run_id, label, kind, magnitude, split, ticks=20, onset=5):
    rows = []
    for t in range(ticks):
        active = t >= onset and kind != "none"
        for region in ("region-a", "region-b"):
            faulted = active and region == "region-a"
            int_extra = ext_extra = int_err = ext_err = 0.0
            if faulted and kind == "path_delay_ms":
                ext_extra = magnitude
            elif faulted and kind == "path_loss":
                ext_err = magnitude
            elif faulted and kind == "service_delay_ms":
                int_extra = ext_extra = magnitude  # users also see a slow server
            elif faulted and kind == "both_errors":
                int_err = ext_err = magnitude
            ip, ie = view(int_extra, int_err)
            ep, ee = view(ext_extra, ext_err)
            rows.append({
                "run_id": run_id, "split": split, "tick_index": t, "region": region,
                "int_p95_ms": ip, "int_error_ratio": ie, "int_samples": PROBES,
                "ext_p95_ms": ep, "ext_error_ratio": ee, "ext_samples": PROBES,
                "label": label if faulted else "healthy",
            })
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="data/ticks.jsonl")
    p.add_argument("--runs", type=int, default=10, help="runs per scenario and magnitude")
    p.add_argument("--seed", type=int, default=7)
    a = p.parse_args()
    random.seed(a.seed)
    rows = []
    for label, kind, train_mags, test_mags in SCENARIOS:
        for split, mags in (("train", train_mags), ("heldout", test_mags)):
            for m in mags:
                for r in range(a.runs):
                    rows += run(f"{kind}-{m}-{split}-r{r:02d}", label, kind, m, split)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    print(f"Wrote {len(rows)} FAKE practice rows to {out}")


if __name__ == "__main__":
    main()
