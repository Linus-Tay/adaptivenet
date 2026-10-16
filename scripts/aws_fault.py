#!/usr/bin/env python3
"""Guarded real EC2 demo HTTP fault via SSM. Schedules automatic recovery FIRST.

No arbitrary target, shell input, Route 53, CloudFront, Kubernetes or networking
mutations. This is an application-availability fault; RIPE ICMP can remain healthy.
Requires AWS approval and --execute --confirm. No write is performed on dry run.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
from adaptivenet.ingestion.witness import append_probe

TARGETS = {
    "region-a": ("ap-southeast-1", "i-0039feaedbd2b3266"),
    "region-b": ("us-east-1", "i-0ea5125bcb2d201ec"),
}
SERVICE = "adaptivenet-demo.service"

def aws_read(args, *, profile):
    p = subprocess.run(["aws", *args, "--profile", profile, "--output", "json", "--no-cli-pager"],
                       capture_output=True, text=True, check=False, timeout=30)
    if p.returncode:
        raise RuntimeError("AWS verification failed; check SSO/Describe permissions")
    try:
        return json.loads(p.stdout)
    except ValueError as exc:
        raise RuntimeError("Invalid AWS JSON response") from exc

def aws_write(args, *, profile):
    p = subprocess.run(["aws", *args, "--profile", profile, "--output", "json", "--no-cli-pager"],
                       capture_output=True, text=True, check=False, timeout=40)
    if p.returncode:
        raise RuntimeError("SSM request failed; check IAM, instance management and SSM agent")
    try:
        return json.loads(p.stdout)
    except ValueError as exc:
        raise RuntimeError("Invalid SSM response") from exc

def fault_script(duration: int) -> str:
    if not isinstance(duration, int) or isinstance(duration, bool) or not 10 <= duration <= 60:
        raise ValueError("Duration must be 10-60 seconds")
    return f"""set -eu
svc={SERVICE}
/usr/bin/systemctl is-active --quiet "$svc" || {{ echo "DEMO_SERVICE_NOT_ACTIVE"; exit 20; }}
# A separate systemd timer restores this exact demo service before it is stopped.
restore_unit="adaptivenet-restore-$(date +%s)"
/usr/bin/systemd-run --unit "$restore_unit" --on-active={duration}s /usr/bin/systemctl start "$svc"
/usr/bin/systemctl stop "$svc"
echo "DEMO_SERVICE_STOPPED;RESTORE_TIMER=$restore_unit;DURATION={duration}s"
"""

def validate_target(profile: str, expected_account: str, region: str):
    if region not in TARGETS or not expected_account.isdecimal() or len(expected_account) != 12:
        raise ValueError("Invalid region or expected AWS account")
    identity = aws_read(["sts", "get-caller-identity"], profile=profile)
    if identity.get("Account") != expected_account:
        raise RuntimeError("AWS account mismatch; refusing live fault")
    aws_region, instance_id = TARGETS[region]
    body = aws_read(["ec2", "describe-instances", "--region", aws_region,
                     "--instance-ids", instance_id], profile=profile)
    try:
        instance = body["Reservations"][0]["Instances"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("Expected EC2 instance not found") from exc
    tags = {entry["Key"]: entry["Value"] for entry in instance.get("Tags", [])
            if "Key" in entry and "Value" in entry}
    if (instance.get("InstanceId") != instance_id
        or instance.get("State", {}).get("Name") != "running"
        or tags.get("Project") != "AdaptiveNet" or tags.get("Environment") != "demo"):
        raise RuntimeError("Instance is not the expected RUNNING tagged AdaptiveNet demo instance")
    return aws_region, instance_id

def verify_ssm_online(profile: str, aws_region: str, instance_id: str) -> None:
    """Reject non-managed or disconnected demo instances before any AWS write."""
    result = aws_read(["ssm", "describe-instance-information", "--region", aws_region,
                       "--filters", "Key=InstanceIds,Values=" + instance_id], profile=profile)
    items = result.get("InstanceInformationList", [])
    if not any(item.get("InstanceId") == instance_id and item.get("PingStatus") == "Online"
               for item in items):
        raise RuntimeError("EC2 not confirmed SSM Online; refusing live fault")

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", choices=sorted(TARGETS), required=True)
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--profile", default="adaptivenet-admin")
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--execute", action="store_true", help="Send REAL SSM command")
    parser.add_argument("--confirm", default="", help="STOP-REGION-A or STOP-REGION-B")
    parser.add_argument("--out", default=str(ROOT / "data" / "probe-events.jsonl"))
    args = parser.parse_args(argv)
    if not 10 <= args.duration <= 60:
        parser.error("duration must be 10-60 seconds")
    try:
        aws_region, instance_id = validate_target(args.profile, args.expected_account, args.region)
        script = fault_script(args.duration)
        print("Target:", args.region, aws_region, instance_id, "account:", args.expected_account)
        print(f"Planned fault: stop {SERVICE} for {args.duration}s; independent systemd restore timer first.")
        print("Note: RIPE ICMP is a different layer and may remain healthy.")
        if not args.execute:
            print("DRY RUN — add --execute --confirm STOP-" + args.region.upper())
            return 0
        required = "STOP-" + args.region.upper()
        if args.confirm != required:
            parser.error("Live request requires --confirm " + required)
        verify_ssm_online(args.profile, aws_region, instance_id)
        payload = aws_write(["ssm", "send-command", "--region", aws_region,
                             "--instance-ids", instance_id, "--document-name", "AWS-RunShellScript",
                             "--comment", "AdaptiveNet bounded demo fault; recovery scheduled before stop",
                             "--timeout-seconds", "90",
                             "--parameters", json.dumps({"commands": [script]})], profile=args.profile)
        command_id = payload.get("Command", {}).get("CommandId")
        if not isinstance(command_id, str) or not command_id:
            raise RuntimeError("No SSM command ID; inspect Systems Manager")
        print("SSM accepted request:", command_id, "(execution and restoration NOT YET verified)")
        print("Check:", "aws ssm get-command-invocation --region", aws_region,
              "--instance-id", instance_id, "--command-id", command_id, "--profile", args.profile)
        append_probe(args.out, {"timestamp": datetime.now(timezone.utc).isoformat(),
                                "scope": args.region, "kind": "aws_fault_requested",
                                "outcome": "ssm_submitted", "duration_seconds": args.duration})
        return 0
    except (ValueError, RuntimeError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print("STOP:", exc, file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
