#!/usr/bin/env python3
"""Offline comparison: no action vs threshold rule vs PID vs ML-guided PID.

A SIMULATION. Nothing here talks to the gateway, AWS or any network; the plant is a
small queueing model written below, so every number it prints is a property of that
model and of the chosen seeds, not a measurement of the real system.

    python experiments/pid_comparison.py                 # table to stdout
    python experiments/pid_comparison.py --out experiments/results/pid-comparison

Plant (per region, per 5 s tick)
    rho      = share_of_traffic * total_rps / capacity
    service  = base_ms / (1 - rho)   (capped at the 3 s client timeout; rho >= 1 sheds 1 - 1/rho as errors)
    faults   = path_delay_ms / path_loss (user path only), service_delay_ms (everywhere),
               capacity_factor, telemetry_gap (status "stale"), null_p95 (histogram unavailable)
Observations use the same shape and p95 index as collectors/internal.py (5 probes).
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import random
import statistics
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "services"))

from adaptivenet.controller.engine import Controller  # noqa: E402
from adaptivenet.controller.pid import PIDConfig, PIDWeightController, advisor_gain_scale  # noqa: E402

TICK_S = 5
BASE_MS = 30.0
TIMEOUT_MS = 3000.0
PROBES = 5
USER_REQUESTS = 100
BAD_LATENCY_MS = 800.0   # engine.py's degraded threshold doubles as the user-facing "bad request" line
MAX_ROUTE_STEP = 25      # controller/routes.py
REGIONS = ("region-a", "region-b")


@dataclass(frozen=True)
class Fault:
    start: float
    end: float
    region: str            # region-a | region-b | both
    kind: str
    value: float = 0.0

    def active(self, t: float, region: str) -> bool:
        return self.start <= t < self.end and self.region in (region, "both")


@dataclass(frozen=True)
class Scenario:
    name: str
    note: str
    duration_s: int
    faults: tuple = ()
    initial_weight: int = 100          # gateway.py starts with weight_a = 100
    total_rps: float = 60.0
    capacity_rps: float = 150.0        # per region
    spike_probability: float = 0.0     # chance per region per tick of one slow probe
    probe_via_gateway: bool = False    # True: a region is only visible through the traffic it receives


SCENARIOS = (
    Scenario("local_latency_30s", "experiments/scenarios/local-latency.json: region-a +1500 ms for 30 s", 300,
             (Fault(60, 90, "region-a", "path_delay_ms", 1500),)),
    Scenario("sustained_latency", "same fault held for 300 s (longer than the gateway's 120 s cap)", 600,
             (Fault(60, 360, "region-a", "path_delay_ms", 1500),)),
    Scenario("capacity_loss", "region-a keeps 40% of its capacity; neither region can carry everything", 900,
             (Fault(60, 900, "region-a", "capacity_factor", 0.4),), initial_weight=60, total_rps=100, capacity_rps=90),
    Scenario("telemetry_gap", "sustained fault, region-a telemetry stale from 120 s to 210 s", 600,
             (Fault(60, 360, "region-a", "path_delay_ms", 1500), Fault(120, 210, "region-a", "telemetry_gap"))),
    Scenario("null_p95", "healthy system, region-a p95 null for 200 s (histogram buckets unavailable)", 400,
             (Fault(60, 260, "region-a", "null_p95"),), initial_weight=50),
    Scenario("noisy_healthy", "no fault; 8% of ticks contain one slow probe per region", 900,
             (), initial_weight=50, spike_probability=0.08),
    Scenario("both_degraded", "both regions +1500 ms service delay for 180 s", 400,
             (Fault(60, 240, "both", "service_delay_ms", 1500),), initial_weight=50),
    Scenario("flapping", "region-a +1500 ms, 20 s on / 40 s off, for 8 minutes", 600,
             tuple(Fault(60 + 60 * i, 80 + 60 * i, "region-a", "path_delay_ms", 1500) for i in range(8))),
    Scenario("drain_blind_spot", "sustained fault; a region is only observable through the traffic it receives", 600,
             (Fault(60, 360, "region-a", "path_delay_ms", 1500),), initial_weight=50, probe_via_gateway=True),
)


# ---- plant --------------------------------------------------------------------
def region_state(sc: Scenario, t: float, region: str, share: float) -> dict:
    get = lambda kind, default: next((f.value for f in sc.faults if f.kind == kind and f.active(t, region)), default)
    has = lambda kind: any(f.kind == kind and f.active(t, region) for f in sc.faults)
    rho = share * sc.total_rps / (sc.capacity_rps * get("capacity_factor", 1.0))
    if rho < 1:
        service, shed = min(TIMEOUT_MS, BASE_MS / (1 - rho)), 0.0
    else:
        service, shed = TIMEOUT_MS, 1 - 1 / rho
    service = min(TIMEOUT_MS, service + get("service_delay_ms", 0.0))
    return {"int_ms": service, "int_err": shed,
            "ext_ms": min(TIMEOUT_MS, service + get("path_delay_ms", 0.0)),
            "ext_err": 1 - (1 - shed) * (1 - get("path_loss", 0.0)),
            "stale": has("telemetry_gap"), "null_p95": has("null_p95")}


def probe(rng: random.Random, median_ms: float, error: float, count: int, spike: bool = False) -> dict:
    """Same output shape and p95 index as collectors/internal.py::sample."""
    if count <= 0:
        return {"status": "ok", "age_seconds": 0, "samples": 0, "error_ratio": None, "p95_ms": None}
    timings, errors = [], 0
    for i in range(count):
        if rng.random() < error:
            errors += 1
        else:
            timings.append(min(TIMEOUT_MS, median_ms * rng.lognormvariate(0, 0.15)) + (900 if spike and i == 0 else 0))
    timings.sort()
    return {"status": "ok", "age_seconds": 0, "samples": count, "error_ratio": errors / count,
            "p95_ms": round(timings[min(len(timings) - 1, int(len(timings) * .95))], 2) if timings else None}


# ---- controllers ----------------------------------------------------------------
class NoAction:
    name = "no_action"
    def decide(self, t, ext, internal, current):
        return current


class ThresholdRule:
    """controller/engine.py exactly as /controller/tick drives it with automation on:
    3 consecutive degraded ticks, one bounded 25 pp step, 120 s cooldown."""
    name = "threshold"
    def __init__(self):
        self.engine, self.errors = Controller(), 0
    def decide(self, t, ext, internal, current):
        try:
            decision = self.engine.evaluate(ext["region-a"], ext["region-b"], current, now=t)
        except TypeError:
            # engine.py compares a null p95 with a number when error_ratio is low. Counted, not
            # hidden: in /controller/tick this would be an HTTP 500 and no decision for that tick.
            self.errors += 1
            return current
        if decision["action"] != "propose_shift":
            return current
        target = decision["target_region_a_percent"]
        self.engine.mark_applied(now=t)
        return max(current - MAX_ROUTE_STEP, min(current + MAX_ROUTE_STEP, target))


class PID:
    name = "pid"
    def __init__(self, config: PIDConfig | None = None, advisor=None, name: str | None = None):
        self.pid, self.advisor = PIDWeightController(config or PIDConfig()), advisor
        self.name = name or ("ml_pid" if advisor else "pid")
    def decide(self, t, ext, internal, current):
        scale = 1.0
        if self.advisor:
            scale = advisor_gain_scale(*(self.advisor(internal[r], ext[r]) for r in REGIONS))
        return self.pid.step(ext["region-a"], ext["region-b"], current, t, gain_scale=scale)["target_region_a_percent"]


# ---- tiny dependency-free diagnosis model (k-nearest neighbours) -------------------
class KnnAdvisor:
    """Per-region diagnosis from internal vs user-path probes, trained on the local,
    git-ignored data/ticks.jsonl (train split only). k-NN keeps it dependency-free;
    confidence is the neighbour vote share and is NOT calibrated."""
    def __init__(self, rows: list[dict], k: int = 7, stride: int = 4):
        self.k = k
        self.points = [(self.vector(r["int_p95_ms"], r["int_error_ratio"], r["ext_p95_ms"], r["ext_error_ratio"]), r["label"])
                       for r in rows[::stride]]

    @staticmethod
    def vector(int_p95, int_err, ext_p95, ext_err):
        return (math.log10(1 + int_p95), 3 * int_err, math.log10(1 + ext_p95), 3 * ext_err)

    def classify(self, int_p95, int_err, ext_p95, ext_err) -> dict | None:
        if None in (int_p95, int_err, ext_p95, ext_err):
            return None  # null is unknown: no advice, the plain PID carries on
        v = self.vector(int_p95, int_err, ext_p95, ext_err)
        nearest = sorted(self.points, key=lambda p: sum((x - y) ** 2 for x, y in zip(v, p[0])))[:self.k]
        votes: dict[str, int] = {}
        for _, label in nearest:
            votes[label] = votes.get(label, 0) + 1
        label = max(sorted(votes), key=votes.get)
        return {"label": label, "confidence": votes[label] / len(nearest)}

    def __call__(self, internal: dict, ext: dict) -> dict | None:
        if internal.get("status") != "ok" or ext.get("status") != "ok":
            return None
        return self.classify(internal.get("p95_ms"), internal.get("error_ratio"), ext.get("p95_ms"), ext.get("error_ratio"))


def load_advisor(path: Path):
    """Returns (advisor, info). Without the dataset there is no ML arm at all,
    rather than a hand-written rule pretending to be one."""
    if not path.exists():
        return None, {"available": False, "reason": f"{path} not found"}
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    train = [r for r in rows if r["split"] == "train"]
    heldout = [r for r in rows if r["split"] == "heldout"][::5]
    advisor = KnnAdvisor(train)
    hits = sum((advisor.classify(r["int_p95_ms"], r["int_error_ratio"], r["ext_p95_ms"], r["ext_error_ratio"]) or {}).get("label") == r["label"]
               for r in heldout)
    return advisor, {"available": True, "train_rows_used": len(advisor.points), "heldout_rows_scored": len(heldout),
                     "heldout_accuracy": round(hits / len(heldout), 4), "model": "k-NN (k=7), uncalibrated"}


# ---- simulation -----------------------------------------------------------------
def run(sc: Scenario, controller, seed: int, trace: bool = False) -> dict:
    rng = random.Random(seed)
    weight = sc.initial_weight
    latencies, bad, total = [], 0, 0
    changes, travel, reversals, last_direction = 0, 0, 0, 0
    first_fault = min((f.start for f in sc.faults if f.kind not in ("telemetry_gap", "null_p95")), default=None)
    first_move, moved_while_unknown, rows = None, 0, []
    for step in range(sc.duration_s // TICK_S):
        t = step * TICK_S
        share = {"region-a": weight / 100, "region-b": 1 - weight / 100}
        state = {r: region_state(sc, t, r, share[r]) for r in REGIONS}
        # what users experienced during this tick, at the weight in force
        for _ in range(USER_REQUESTS):
            s = state["region-a" if rng.random() * 100 < weight else "region-b"]
            total += 1
            if rng.random() < s["ext_err"]:
                bad += 1
                continue
            latency = min(TIMEOUT_MS, s["ext_ms"] * rng.lognormvariate(0, 0.15))
            latencies.append(latency)
            bad += latency > BAD_LATENCY_MS
        ext, internal = {}, {}
        for r in REGIONS:
            s = state[r]
            spike = rng.random() < sc.spike_probability
            count = PROBES if not sc.probe_via_gateway else sum(rng.random() < share[r] for _ in range(2 * PROBES))
            ext[r] = probe(rng, s["ext_ms"], s["ext_err"], count, spike)
            internal[r] = probe(rng, s["int_ms"], s["int_err"], PROBES)
            if s["null_p95"]:
                ext[r]["p95_ms"] = None
            if s["stale"]:
                ext[r] = {"status": "stale"}
        unknown = any(ext[r].get("status") != "ok" or ext[r].get("p95_ms") is None and not ext[r].get("error_ratio")
                      for r in REGIONS)
        new = int(controller.decide(t, ext, internal, weight))
        if new != weight:
            direction = 1 if new > weight else -1
            changes += 1
            travel += abs(new - weight)
            reversals += last_direction != 0 and direction != last_direction
            last_direction = direction
            moved_while_unknown += unknown
            if first_move is None and first_fault is not None and t >= first_fault:
                first_move = t - first_fault
        if trace:
            rows.append({"t": t, "weight_a": weight, "ext_p95_a": ext["region-a"].get("p95_ms"),
                         "ext_p95_b": ext["region-b"].get("p95_ms")})
        weight = new
    ordered = sorted(latencies)
    return {"bad_request_pct": 100 * bad / total,
            "mean_latency_ms": statistics.fmean(latencies) if latencies else None,
            "p95_latency_ms": ordered[int(len(ordered) * .95)] if ordered else None,
            "weight_changes": changes, "travel_pp": travel, "reversals": reversals,
            "first_move_s": first_move, "final_weight_a": weight,
            "moves_while_unknown": moved_while_unknown,
            "controller_errors": getattr(controller, "errors", 0), "trace": rows}


def aggregate(results: list[dict]) -> dict:
    def mean(key):
        values = [r[key] for r in results if r[key] is not None]
        return round(statistics.fmean(values), 2) if values else None
    out = {k: mean(k) for k in ("bad_request_pct", "mean_latency_ms", "p95_latency_ms", "weight_changes",
                                "travel_pp", "reversals", "first_move_s", "moves_while_unknown", "controller_errors")}
    out["final_weight_a_median"] = statistics.median(r["final_weight_a"] for r in results)
    out["runs_that_moved"] = sum(r["weight_changes"] > 0 for r in results)
    return out


def controllers(advisor, config: PIDConfig | None = None):
    made = [NoAction, ThresholdRule, lambda: PID(config)]
    if advisor:
        made.append(lambda: PID(config, advisor))
    return made


def compare(seeds: int, advisor, config: PIDConfig | None = None, scenarios=SCENARIOS) -> dict:
    table = {}
    for sc in scenarios:
        table[sc.name] = {"note": sc.note, "controllers": {}}
        for make in controllers(advisor, config):
            runs = [run(sc, make(), seed) for seed in range(seeds)]
            table[sc.name]["controllers"][make().name] = aggregate(runs)
    return table


def gain_sweep(seeds: int) -> dict:
    """How sensitive is the PID to its gains on the non-linear capacity scenario?"""
    sc = next(s for s in SCENARIOS if s.name == "capacity_loss")
    base, out = PIDConfig(), {}
    for factor in (0.25, 0.5, 1, 2, 4, 8):
        cfg = PIDConfig(kp=base.kp * factor, ki=base.ki * factor)
        out[f"kp,ki x{factor}"] = aggregate([run(sc, PID(cfg), seed) for seed in range(seeds)])
    out["I only (kp=0)"] = aggregate([run(sc, PID(PIDConfig(kp=0)), seed) for seed in range(seeds)])
    out["with D (kd=20)"] = aggregate([run(sc, PID(PIDConfig(kd=20)), seed) for seed in range(seeds)])
    return out


def variants(seeds: int) -> dict:
    """PID options on the scenarios where they matter."""
    pick = lambda name: next(s for s in SCENARIOS if s.name == name)
    cases = {"flapping / pid": ("flapping", PIDConfig()),
             "flapping / I only (kp=0)": ("flapping", PIDConfig(kp=0)),
             "noisy_healthy / I only (kp=0)": ("noisy_healthy", PIDConfig(kp=0)),
             "sustained_latency / failback 0.2 pp/s to 100": ("sustained_latency", PIDConfig(failback_rate_pct_per_s=0.2, home_weight=100)),
             "sustained_latency / canary floor 5%": ("sustained_latency", PIDConfig(min_weight=5)),
             "sustained_latency / 60 s sample interval": ("sustained_latency", PIDConfig(sample_interval_s=60)),
             "drain_blind_spot / failback 0.2 pp/s to 50": ("drain_blind_spot", PIDConfig(failback_rate_pct_per_s=0.2)),
             "drain_blind_spot / failback + canary floor 30%": ("drain_blind_spot", PIDConfig(failback_rate_pct_per_s=0.2, min_weight=30))}
    return {label: aggregate([run(pick(name), PID(cfg), seed) for seed in range(seeds)]) for label, (name, cfg) in cases.items()}


def markdown(table: dict, sweep: dict, extra: dict, info: dict, seeds: int) -> str:
    lines = ["# PID weight controller: simulated comparison", "",
             f"Simulation only ({seeds} seeds per cell, 5 s ticks). Numbers describe the toy plant in "
             "`experiments/pid_comparison.py`, not the real gateway or AWS.", "",
             f"ML advisor: `{json.dumps(info)}`", ""]
    head = "| controller | bad req % | mean ms | p95 ms | moves | travel pp | reversals | first move s | final A % | runs moved | moves on unknown | ctrl errors |"
    for name, entry in table.items():
        lines += [f"## {name}", "", entry["note"], "", head, "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for ctrl, m in entry["controllers"].items():
            lines.append(f"| {ctrl} | {m['bad_request_pct']} | {m['mean_latency_ms']} | {m['p95_latency_ms']} | {m['weight_changes']} | "
                         f"{m['travel_pp']} | {m['reversals']} | {m['first_move_s']} | {m['final_weight_a_median']} | {m['runs_that_moved']}/{seeds} | {m['moves_while_unknown']} | {m['controller_errors']} |")
        lines.append("")
    lines += ["## PID gain sweep on capacity_loss (kp and ki scaled together)", "",
              "| gain | bad req % | p95 ms | moves | travel pp | reversals | final A % |", "|---|---|---|---|---|---|---|"]
    for factor, m in sweep.items():
        lines.append(f"| {factor} | {m['bad_request_pct']} | {m['p95_latency_ms']} | {m['weight_changes']} | {m['travel_pp']} | "
                     f"{m['reversals']} | {m['final_weight_a_median']} |")
    lines += ["", "## PID variants", "", "| scenario / variant | bad req % | p95 ms | moves | travel pp | reversals | final A % |",
              "|---|---|---|---|---|---|---|"]
    for label, m in extra.items():
        lines.append(f"| {label} | {m['bad_request_pct']} | {m['p95_latency_ms']} | {m['weight_changes']} | {m['travel_pp']} | "
                     f"{m['reversals']} | {m['final_weight_a_median']} |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seeds", type=int, default=20)
    ap.add_argument("--ticks", default=str(ROOT / "data/ticks.jsonl"), help="labelled ticks for the ML advisor")
    ap.add_argument("--out", help="directory for summary.md, results.json and traces.json")
    args = ap.parse_args(argv)
    advisor, info = load_advisor(Path(args.ticks))
    table, sweep, extra = compare(args.seeds, advisor), gain_sweep(args.seeds), variants(args.seeds)
    text = markdown(table, sweep, extra, info, args.seeds)
    print(text)
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "summary.md").write_text(text)
        (out / "results.json").write_text(json.dumps({"seeds": args.seeds, "advisor": info, "scenarios": table, "gain_sweep": sweep, "variants": extra}, indent=2))
        traces = {sc.name: {make().name: run(sc, make(), 0, trace=True)["trace"] for make in controllers(advisor)}
                  for sc in SCENARIOS if sc.name in ("capacity_loss", "sustained_latency", "flapping")}
        (out / "traces.json").write_text(json.dumps(traces))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
