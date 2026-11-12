"""Fault-injection CLI for the local demo lab. Never touches AWS or EKS.

  python -m demo.faultctl latency     --region sg --ms 700  [--duration 180]
  python -m demo.faultctl loss        --region sg --pct 50
  python -m demo.faultctl app-5xx     --region us --rate 0.5
  python -m demo.faultctl cpu         --region sg
  python -m demo.faultctl region-down --region sg
  python -m demo.faultctl clear
  python -m demo.faultctl reset-route         # send 100% back to region-a
  python -m demo.faultctl status

Group lab (demo/run.sh lab-up), same faults as the dashboard buttons but longer:
  python -m demo.faultctl lab-latency --region sg --ms 1500 [--duration 120]
  python -m demo.faultctl lab-errors  --region sg --rate 1.0
  python -m demo.faultctl lab-clear
  python -m demo.faultctl lab-reset-route

Network faults (latency, loss) act on the emulated external path only, so
AWS-style internal metrics stay healthy: the gray-failure case. App faults
(app-5xx, cpu, region-down) act inside the origin. Every command is appended
to ``fault-log.jsonl`` as an operator action. It is NOT a label: whether a
failure happened is decided from the telemetry, never from this log.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

import httpx

ALIASES = {"sg": "region-a", "singapore": "region-a", "region-a": "region-a", "a": "region-a",
           "us": "region-b", "virginia": "region-b", "region-b": "region-b", "b": "region-b"}


def env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def endpoints() -> dict:
    return {"edge": env("DEMO_EDGE_URL", "http://127.0.0.1:8191"),
            "gateway": env("DEMO_GATEWAY_URL", "http://127.0.0.1:8180"),
            "lab-gateway": env("LAB_GATEWAY_URL", "http://127.0.0.1:8080"),
            "region-a": env("DEMO_REGION_A_URL", "http://127.0.0.1:8181"),
            "region-b": env("DEMO_REGION_B_URL", "http://127.0.0.1:8182")}


def token() -> str:
    value = os.environ.get("LAB_TOKEN")
    if not value:
        env_file = Path(__file__).with_name(".env.demo")
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("LAB_TOKEN="):
                    value = line.split("=", 1)[1].strip()
    if not value:
        raise SystemExit("LAB_TOKEN not set (run demo/run.sh up first, or export it)")
    return value


def plan(args: argparse.Namespace) -> list[tuple[str, str, str, dict | None]]:
    """Return (method, service, path, body) calls for a command. Pure, for tests."""
    e = []
    region = ALIASES.get((args.region or "").lower()) if getattr(args, "region", None) else None
    if args.command in ("latency", "loss", "app-5xx", "cpu", "region-down", "lab-latency", "lab-errors") and region is None:
        raise SystemExit("--region sg|us is required")
    d = args.duration
    if args.command == "latency":
        e.append(("POST", "edge", "/admin/faults", {"region": region, "latency_ms": args.ms, "duration_seconds": d}))
    elif args.command == "loss":
        e.append(("POST", "edge", "/admin/faults", {"region": region, "loss_pct": args.pct, "duration_seconds": d}))
    elif args.command == "app-5xx":
        e.append(("POST", region, "/lab/app-fault", {"kind": "app_5xx", "error_rate": args.rate, "duration_seconds": d}))
    elif args.command == "cpu":
        e.append(("POST", region, "/lab/app-fault", {"kind": "cpu_burn", "duration_seconds": d}))
    elif args.command == "region-down":
        e.append(("POST", region, "/lab/app-fault", {"kind": "down", "duration_seconds": d}))
        e.append(("POST", "edge", "/admin/faults", {"region": region, "loss_pct": 100, "duration_seconds": d}))
    elif args.command == "lab-latency":
        e.append(("POST", "lab-gateway", "/admin/faults", {"region": region, "latency_ms": args.ms, "duration_seconds": min(d, 120)}))
    elif args.command == "lab-errors":
        e.append(("POST", "lab-gateway", "/admin/faults", {"region": region, "error_rate": args.rate, "duration_seconds": min(d, 120)}))
    elif args.command == "lab-clear":
        e.append(("DELETE", "lab-gateway", "/admin/faults", None))
    elif args.command == "lab-reset-route":
        e.append(("GET", "lab-gateway", "/admin/state", None))
    elif args.command == "clear":
        e += [("DELETE", "edge", "/admin/faults", None), ("DELETE", "region-a", "/lab/app-fault", None),
              ("DELETE", "region-b", "/lab/app-fault", None)]
    elif args.command == "reset-route":
        e.append(("GET", "gateway", "/admin/state", None))  # read, then PUT with the expected weight
    elif args.command == "status":
        e += [("GET", "edge", "/admin/faults", None), ("GET", "gateway", "/admin/state", None),
              ("GET", "region-a", "/lab/metrics", None), ("GET", "region-b", "/lab/metrics", None)]
    return e


def log_command(argv: list[str]) -> None:
    folder = Path(env("DEMO_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data" / "demo-live")))
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "fault-log.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": time.time(), "command": " ".join(argv),
                            "note": "operator action; not a label or proof of failure"}) + "\n")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m demo.faultctl", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    for name, extra in (("latency", ("--ms", int, 700)), ("loss", ("--pct", float, 50)),
                        ("app-5xx", ("--rate", float, 0.5)), ("cpu", None), ("region-down", None),
                        ("lab-latency", ("--ms", int, 1500)), ("lab-errors", ("--rate", float, 1.0))):
        s = sub.add_parser(name)
        s.add_argument("--region", required=True, help="sg (region-a) or us (region-b)")
        s.add_argument("--duration", type=int, default=120 if name.startswith("lab-") else 180,
                       help="seconds (max 900; lab gateway max 120)")
        if extra:
            s.add_argument(extra[0], type=extra[1], default=extra[2])
    for name in ("clear", "reset-route", "status", "lab-clear", "lab-reset-route"):
        s = sub.add_parser(name)
        s.set_defaults(duration=0, region=None)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = parser().parse_args(argv)
    urls, headers = endpoints(), {"X-Lab-Token": token()}
    with httpx.Client(timeout=5, trust_env=False) as client:
        results = []
        for method, service, path, body in plan(args):
            try:
                r = client.request(method, urls[service] + path, json=body, headers=headers)
                results.append({"call": f"{method} {service}{path}", "status": r.status_code, "body": r.json()})
            except (httpx.HTTPError, ValueError) as exc:
                results.append({"call": f"{method} {service}{path}", "error": str(exc)})
        if args.command in ("reset-route", "lab-reset-route") and results and results[0].get("status") == 200:
            current = results[0]["body"]["region_a_percent"]
            gw = urls["lab-gateway" if args.command.startswith("lab-") else "gateway"]
            r = client.put(gw + "/admin/routes", headers=headers,
                           json={"expected_region_a_percent": current, "region_a_percent": 100})
            results.append({"call": "PUT gateway/admin/routes", "status": r.status_code, "body": r.json()})
    if args.command != "status":
        log_command(argv)
    print(json.dumps(results, indent=2))
    return 0 if all(r.get("status", 500) < 400 for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
