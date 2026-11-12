#!/usr/bin/env python3
"""Pre-registered, randomized packet-loss trial campaign (operator-approved).

`plan` writes a seeded schedule BEFORE any trial runs. `run` executes it by calling
the existing guarded `scripts/native_network_fault_trial.py` (automatic rollback is
armed on the instance before any iptables rule). Each slot impairs ONE region while
the other region is untouched and serves as the same-time healthy control.

Abort at any time: create the STOP file named in the plan. Rollbacks already armed
on the instances still fire on their own timers.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import random
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
TRIAL = ROOT / "scripts" / "native_network_fault_trial.py"
LOSS_LEVELS = (10, 15, 20, 25, 30, 35)
DURATION = 960          # 4 RIPE rounds at 240 s
CYCLE = 50 * 60         # region-a slot at +0, region-b slot at +25 min
CONTROL_SETTLE = 4 * 60  # control window starts 4 min into the other region's slot


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def plan(seed: int, start: datetime, cycles: int, ripe: dict[str, int]) -> dict:
    rng = random.Random(seed)
    slots = []
    for c in range(cycles):
        order = ["region-a", "region-b"]
        if rng.random() < .5:
            order.reverse()  # randomize which region goes first in each cycle
        for k, region in enumerate(order):
            jitter = rng.randint(0, 180)
            begin = start + timedelta(seconds=c * CYCLE + k * CYCLE // 2 + jitter)
            other = "region-b" if region == "region-a" else "region-a"
            slots.append({"slot": len(slots), "region": region, "control_region": other,
                          "planned_start_utc": iso(begin), "duration_seconds": DURATION,
                          "loss_percent": rng.choice(LOSS_LEVELS),
                          "control_window_utc": [iso(begin + timedelta(seconds=CONTROL_SETTLE)),
                                                 iso(begin + timedelta(seconds=DURATION))],
                          "ripe_measurement_id": ripe[region]})
    return {"schema": "adaptivenet.ripe_trial_campaign_plan.v1", "seed": seed,
            "created_utc": iso(datetime.now(timezone.utc)), "slots": slots,
            "frozen_analysis": "docs/ml/RIPE_TRIALS_PROTOCOL.md",
            "stop_file": str(ROOT / "data" / "ml-results" / "native" / "ripe-campaign" / "STOP")}


def run(plan_path: Path, profile: str, account: str, out_dir: Path) -> int:
    p = json.loads(plan_path.read_text(encoding="utf-8"))
    log = out_dir / "campaign-log.jsonl"
    out_dir.mkdir(parents=True, exist_ok=True)
    done = set()
    if log.exists():
        done = {json.loads(l)["slot"] for l in log.read_text().splitlines() if l.strip()}
    stop = Path(p["stop_file"])
    for s in p["slots"]:
        if s["slot"] in done:
            continue
        when = datetime.fromisoformat(s["planned_start_utc"])
        while datetime.now(timezone.utc) < when:
            if stop.exists():
                print("STOP file present; campaign halted"); return 3
            time.sleep(min(20, max(1, (when - datetime.now(timezone.utc)).total_seconds())))
        if stop.exists():
            print("STOP file present; campaign halted"); return 3
        late = (datetime.now(timezone.utc) - when).total_seconds()
        rec = {"slot": s["slot"], "region": s["region"], "planned_start_utc": s["planned_start_utc"],
               "loss_percent": s["loss_percent"], "invoked_utc": iso(datetime.now(timezone.utc)),
               "late_seconds": late}
        if late > 300:
            rec["status"] = "skipped_late"  # never compress the schedule into overlapping windows
        else:
            cmd = [sys.executable, str(TRIAL), "run", "--profile", profile, "--region", s["region"],
                   "--expected-account", account, "--duration", str(s["duration_seconds"]),
                   "--loss-percent", str(s["loss_percent"]), "--ripe-measurement-id", str(s["ripe_measurement_id"]),
                   "--execute", "--confirm", "NETWORK-IMPAIR-" + s["region"].upper(), "--out-dir", str(out_dir / "trials")]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
            rec.update({"returncode": r.returncode, "stdout_tail": r.stdout[-1500:], "stderr_tail": r.stderr[-800:]})
            rec["status"] = "submitted" if r.returncode == 0 else "refused_or_failed"
            if r.returncode == 0:
                trial = next((ln.split(":", 1)[1].strip() for ln in r.stdout.splitlines()
                              if ln.startswith("Experiment request evidence:")), None)
                rec["trial_record"] = trial
                time.sleep(90)
                chk = subprocess.run([sys.executable, str(TRIAL), "check", "--profile", profile,
                                      "--expected-account", account, "--trial", trial],
                                     capture_output=True, text=True, timeout=120)
                rec["check_returncode"] = chk.returncode
                rec["check_stdout_tail"] = chk.stdout[-1200:]
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
        print(json.dumps({k: rec.get(k) for k in ("slot", "region", "loss_percent", "status", "check_returncode")}), flush=True)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("plan")
    a.add_argument("--seed", type=int, required=True)
    a.add_argument("--start-utc", required=True)
    a.add_argument("--cycles", type=int, default=10)
    a.add_argument("--ripe-a", type=int, required=True)
    a.add_argument("--ripe-b", type=int, required=True)
    a.add_argument("--out", type=Path, required=True)
    b = sub.add_parser("run")
    b.add_argument("--plan", type=Path, required=True)
    b.add_argument("--profile", default="adaptivenet-ml")
    b.add_argument("--expected-account", required=True)
    b.add_argument("--out-dir", type=Path, default=ROOT / "data" / "ml-results" / "native" / "ripe-campaign")
    args = ap.parse_args(argv)
    if args.cmd == "plan":
        start = datetime.fromisoformat(args.start_utc).astimezone(timezone.utc)
        p = plan(args.seed, start, args.cycles, {"region-a": args.ripe_a, "region-b": args.ripe_b})
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(p, indent=2) + "\n", encoding="utf-8")
        print(f"{len(p['slots'])} slots, first {p['slots'][0]['planned_start_utc']}, last {p['slots'][-1]['planned_start_utc']}")
        return 0
    return run(args.plan, args.profile, args.expected_account, args.out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
