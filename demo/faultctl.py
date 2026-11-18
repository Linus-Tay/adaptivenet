"""Fault CLI for the local demo (demo/run.sh lab-up). Never touches AWS or EKS.

  python -m demo.faultctl lab-latency --region sg --ms 1500 [--duration 120]
  python -m demo.faultctl lab-errors  --region sg --rate 1.0      # HTTP 503
  python -m demo.faultctl lab-loss    --region sg --pct 80        # packet loss
  python -m demo.faultctl app-5xx     --region us --rate 0.5      # inside the origin
  python -m demo.faultctl cpu         --region sg
  python -m demo.faultctl region-down --region sg
  python -m demo.faultctl lab-clear
  python -m demo.faultctl lab-reset-route                         # 100% back to Singapore
  python -m demo.faultctl status

Gateway faults (latency, errors, loss) sit on the path in front of a region, so its
own metrics stay healthy: the gray-failure case. App faults (app-5xx, cpu,
region-down) act inside the origin and are visible to the AWS-style view. Gateway
faults last at most 120 s. Every command is logged to ``fault-log.jsonl`` as an
operator action, never as a label.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

import httpx

REGIONAL = ("lab-latency", "lab-errors", "lab-loss", "app-5xx", "cpu", "region-down")
ALIASES = {"sg": "region-a", "singapore": "region-a", "region-a": "region-a", "a": "region-a",
           "us": "region-b", "virginia": "region-b", "region-b": "region-b", "b": "region-b"}


def env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def endpoints() -> dict:
    return {"lab-gateway": env("LAB_GATEWAY_URL", "http://127.0.0.1:8080"),
            "region-a": env("DEMO_REGION_A_URL", "http://127.0.0.1:8101"),
            "region-b": env("DEMO_REGION_B_URL", "http://127.0.0.1:8102")}


def token() -> str:
    value = os.environ.get("LAB_TOKEN")
    if not value:
        env_file = Path(__file__).with_name(".env.demo")
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("LAB_TOKEN="):
                    value = line.split("=", 1)[1].strip()
    if not value:
        raise SystemExit("LAB_TOKEN not set (run demo/run.sh lab-up first, or export it)")
    return value


def plan(args: argparse.Namespace) -> list[tuple[str, str, str, dict | None]]:
    """Return (method, service, path, body) calls for a command. Pure, for tests."""
    e = []
    region = ALIASES.get((args.region or "").lower()) if getattr(args, "region", None) else None
    if args.command in REGIONAL and region is None:
        raise SystemExit("--region sg|us is required")
    d = min(args.duration, 120)
    gateway = lambda **fault: ("POST", "lab-gateway", "/admin/faults",
                               {"region": region, **fault, "duration_seconds": d})
    if args.command == "lab-latency":
        e.append(gateway(latency_ms=args.ms))
    elif args.command == "lab-errors":
        e.append(gateway(error_rate=args.rate))
    elif args.command == "lab-loss":
        e.append(gateway(loss_pct=args.pct))
    elif args.command == "app-5xx":
        e.append(("POST", region, "/lab/app-fault", {"kind": "app_5xx", "error_rate": args.rate, "duration_seconds": d}))
    elif args.command == "cpu":
        e.append(("POST", region, "/lab/app-fault", {"kind": "cpu_burn", "duration_seconds": d}))
    elif args.command == "region-down":
        e.append(("POST", region, "/lab/app-fault", {"kind": "down", "duration_seconds": d}))
        e.append(gateway(loss_pct=100))
    elif args.command == "lab-clear":
        e += [("DELETE", "lab-gateway", "/admin/faults", None), ("DELETE", "region-a", "/lab/app-fault", None),
              ("DELETE", "region-b", "/lab/app-fault", None)]
    elif args.command == "lab-reset-route":
        e.append(("GET", "lab-gateway", "/admin/state", None))  # read, then PUT with the expected weight
    elif args.command == "status":
        e += [("GET", "lab-gateway", "/admin/state", None),
              ("GET", "region-a", "/lab/metrics", None), ("GET", "region-b", "/lab/metrics", None)]
    return e


def log_command(argv: list[str]) -> None:
    folder = Path(env("DEMO_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data" / "lab-live")))
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "fault-log.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": time.time(), "command": " ".join(argv),
                            "note": "operator action; not a label or proof of failure"}) + "\n")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m demo.faultctl", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    for name, extra in (("lab-latency", ("--ms", int, 1500)), ("lab-errors", ("--rate", float, 1.0)),
                        ("lab-loss", ("--pct", float, 80)), ("app-5xx", ("--rate", float, 0.5)),
                        ("cpu", None), ("region-down", None)):
        s = sub.add_parser(name)
        s.add_argument("--region", required=True, help="sg (region-a) or us (region-b)")
        s.add_argument("--duration", type=int, default=120, help="seconds (max 120)")
        if extra:
            s.add_argument(extra[0], type=extra[1], default=extra[2])
    for name in ("lab-clear", "lab-reset-route", "status"):
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
        if args.command == "lab-reset-route" and results and results[0].get("status") == 200:
            current = results[0]["body"]["region_a_percent"]
            gw = urls["lab-gateway"]
            r = client.put(gw + "/admin/routes", headers=headers,
                           json={"expected_region_a_percent": current, "region_a_percent": 100})
            results.append({"call": "PUT gateway/admin/routes", "status": r.status_code, "body": r.json()})
    if args.command != "status":
        log_command(argv)
    print(json.dumps(results, indent=2))
    return 0 if all(r.get("status", 500) < 400 for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
