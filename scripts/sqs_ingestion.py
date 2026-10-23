#!/usr/bin/env python3
"""Actual AdaptiveNet SQS producer/consumer (use existing collectors and feature model).

publish-once / publish-watch: AWS/RIPE/CloudFront -> durable outbox -> SQS
consume-once / consume-watch: SQS -> SQLite events + features -> acknowledge
status: read-only queue backlog / visibility / redrive information
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))

from adaptivenet.ingestion.cli import collect_source_poll
from adaptivenet.ingestion.config import read_env_file
from adaptivenet.ingestion.sqs_transport import (
    Outbox, encode_poll, resolve_queue_url, sqs_client, receive_one, queue_health,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("publish-once", "publish-watch",
                                           "consume-once", "consume-watch", "status"))
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE") or "adaptivenet-admin")
    parser.add_argument("--queue-url", default=os.environ.get("ADAPTIVENET_SQS_QUEUE_URL"))
    parser.add_argument("--account-id", default=os.environ.get("ADAPTIVENET_AWS_ACCOUNT_ID", "468354413374"))
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--db", default="data/adaptivenet-ingestion.db")
    parser.add_argument("--outbox", default="data/sqs-outbox")
    parser.add_argument("--interval", type=int, default=120)
    parser.add_argument("--wait-seconds", type=int, default=20)
    args = parser.parse_args(argv)
    if args.interval < 60 or not 0 <= args.wait_seconds <= 20:
        parser.error("interval >= 60s; wait-seconds between 0 and 20")
    # Explicit commands are required. The default ingestion.py watcher still
    # uses its original direct SQLite mode; do not run BOTH ingestion modes.
    settings = read_env_file(args.env_file)
    queue_candidate = args.queue_url or settings.get("ADAPTIVENET_SQS_QUEUE_URL")
    expected_account = os.environ.get("ADAPTIVENET_AWS_ACCOUNT_ID") or settings.get("ADAPTIVENET_AWS_ACCOUNT_ID") or args.account_id
    client = sqs_client(args.profile)
    queue_url = resolve_queue_url(client, queue_candidate, expected_account)
    outbox = Outbox(args.outbox) if args.command.startswith("publish-") else None

    def publish():
        # Replay ALL already staged messages before collecting new source data.
        pending = outbox.flush(client, queue_url)
        collected, cf_id, collected_at = collect_source_poll(profile=args.profile,
                                                               env_file=args.env_file)
        payload = encode_poll(collected.observations, collected.errors,
                              collected.status, cf_id, collected_at)
        staged = outbox.stage(payload)
        sent = outbox.flush(client, queue_url)
        result = {"mode": "queued_producer", "pending_replayed": pending,
                  "enqueued": sent, "observations": len(collected.observations),
                  "outbox_path": str(staged.parent)}
        print(json.dumps(result), flush=True)

    def consume():
        result = receive_one(client, queue_url, args.db, wait_seconds=args.wait_seconds)
        if result["received"]:
            print(json.dumps({"mode": "queued_consumer", **result}), flush=True)
        return result

    if args.command == "status":
        status = queue_health(client, queue_url)
        status["outbox_files"] = len(list(Path(args.outbox).glob("*.json")))
        print(json.dumps(status, indent=2), flush=True)
        return 0
    if args.command == "publish-once":
        publish()
        return 0
    if args.command == "consume-once":
        consume()
        return 0

    print(f"AdaptiveNet SQS {args.command} started; Ctrl+C to stop; queue={queue_url}", flush=True)
    try:
        while True:
            before = time.monotonic()
            try:
                if args.command == "publish-watch":
                    publish()
                else:
                    consume()
            except Exception as exc:
                # Do not acknowledge messages on consumer failure; they will
                # reappear after SQS visibility timeout and eventually go to DLQ.
                print(json.dumps({"status": "retrying", "error_type": type(exc).__name__}),
                      file=sys.stderr, flush=True)
                time.sleep(3)
            if args.command == "publish-watch":
                time.sleep(max(0, args.interval - (time.monotonic() - before)))
    except KeyboardInterrupt:
        print("SQS worker stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
