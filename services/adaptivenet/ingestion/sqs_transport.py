"""SQS v1 transport for existing source polls. No model or AWS mitigation here.

One complete poll per message, immutable source timestamps, at-least-once delivery.
The producer keeps a local durable outbox until SendMessage confirms success;
the consumer deletes the receipt ONLY after SQLite event+snapshot persistence.
"""
from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
import json
import os
import re
import time
from typing import Any

from .models import Observation
from .store import Store
from .features import make_snapshot, AMP_CLOCK_SKEW_SEC

CONTRACT = "adaptivenet.telemetry_poll.v1"
QUEUE_NAME = "adaptivenet-dev-telemetry"
AWS_REGION = "ap-southeast-1"
MAX_MESSAGE_BYTES = 200_000  # deliberately below SQS 1-MiB API maximum
MAX_OUTBOX_FILES = 1000
QUEUE_URL_RE = re.compile(r"^https://sqs\.ap-southeast-1\.amazonaws\.com/(\d{12})/adaptivenet-dev-telemetry$")


def encode_poll(observations: list[Observation], errors: list[str], status: str,
                cloudfront_distribution: str | None, collected_at: float) -> str:
    if not isinstance(collected_at, (float, int)) or isinstance(collected_at, bool) or not 0 < collected_at < 1e12:
        raise ValueError("Invalid collection time")
    if not isinstance(status, str) or len(status) > 80:
        raise ValueError("Invalid poll status")
    if not isinstance(errors, list) or len(errors) > 100 or any(not isinstance(e, str) or len(e) > 160 for e in errors):
        raise ValueError("Invalid collection errors")
    if len(observations) > 3000:
        raise ValueError("Too many observations for one poll")
    core = {
        "contract_version": CONTRACT, "collected_at": collected_at,
        "source_status": status, "source_errors": errors,
        "cloudfront_distribution": cloudfront_distribution,
        "observations": [{"event_id": e.event_id, **asdict(e)} for e in observations],
    }
    canonical = json.dumps(core, sort_keys=True, separators=(",", ":"), allow_nan=False)
    core["batch_id"] = sha256(canonical.encode("utf-8")).hexdigest()
    body = json.dumps(core, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(body.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ValueError("Poll exceeds 200KB; fail closed rather than dropping part of a batch")
    return body


def decode_poll(body: str) -> tuple[dict, list[Observation]]:
    if not isinstance(body, str) or len(body.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ValueError("Oversized or invalid SQS body")
    message = json.loads(body)
    if not isinstance(message, dict) or message.get("contract_version") != CONTRACT:
        raise ValueError("Unsupported telemetry contract")
    ident = message.get("batch_id")
    if not isinstance(ident, str) or not re.fullmatch(r"[a-f0-9]{64}", ident):
        raise ValueError("Missing batch ID")
    unsigned = {k: v for k, v in message.items() if k != "batch_id"}
    calculated = sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    if ident != calculated:
        raise ValueError("Poll contents differ from batch checksum")
    timestamp = message.get("collected_at")
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not 0 < timestamp < 1e12:
        raise ValueError("Invalid collection timestamp")
    if timestamp > time.time() + 300:
        raise ValueError("Collection timestamp is too far in the future")
    status, errors, raw = message.get("source_status"), message.get("source_errors"), message.get("observations")
    if not isinstance(status, str) or len(status) > 80 or not isinstance(errors, list) or len(errors) > 100:
        raise ValueError("Invalid collection metadata")
    if any(not isinstance(x, str) or len(x) > 160 for x in errors):
        raise ValueError("Invalid source error metadata")
    if not isinstance(raw, list) or len(raw) > 3000:
        raise ValueError("Invalid observations list")
    cf = message.get("cloudfront_distribution")
    if cf is not None and (not isinstance(cf, str) or not re.fullmatch(r"E[A-Z0-9]{8,20}", cf)):
        raise ValueError("Invalid CloudFront distribution")
    observations = []
    for record in raw:
        if not isinstance(record, dict):
            raise ValueError("Invalid observation")
        stored_id = record.get("event_id")
        fields = {k: v for k, v in record.items() if k != "event_id"}
        item = Observation(**fields)
        if item.event_id != stored_id:
            raise ValueError("Observation hash mismatch")
        observations.append(item)
    return message, observations


def resolve_queue_url(client: Any, queue_url: str | None = None, account_id: str | None = None) -> str:
    """Use one fixed SG dev queue; never accept an arbitrary queue URL."""
    if not queue_url:
        parameters: dict[str, str] = {"QueueName": QUEUE_NAME}
        if account_id:
            parameters["QueueOwnerAWSAccountId"] = account_id
        queue_url = client.get_queue_url(**parameters)["QueueUrl"]
    match = QUEUE_URL_RE.fullmatch(queue_url)
    if not match or (account_id and match.group(1) != account_id):
        raise ValueError("Expected AdaptiveNet Singapore telemetry SQS URL")
    return queue_url


def sqs_client(profile: str | None = None):
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("Install queue support: python -m pip install -e '.[queue]'") from exc
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    return session.client("sqs", region_name=AWS_REGION)


class Outbox:
    """Durably spool until SQS confirms send; local retry after networking outages."""
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)

    def pending(self) -> list[Path]:
        return sorted(self.path.glob("*.json"))

    def stage(self, body: str) -> Path:
        message, _ = decode_poll(body)
        existing = self.pending()
        if len(existing) >= MAX_OUTBOX_FILES:
            raise RuntimeError("SQS outbox full; restore connectivity before continuing")
        path = self.path / (message["batch_id"] + ".json")
        if path.exists():
            return path
        # A crash may leave an old .tmp; a unique name never prevents replay.
        temporary = self.path / (message["batch_id"] + f".{os.getpid()}.{time.time_ns()}.tmp")
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write(body)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            # Directory fsync prevents acknowledged staging from vanishing on hard reboot.
            if hasattr(os, "O_DIRECTORY"):
                dirfd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(dirfd)
                finally:
                    os.close(dirfd)
        finally:
            if temporary.exists():
                temporary.unlink()
        return path

    def flush(self, client: Any, queue_url: str) -> int:
        sent = 0
        for path in self.pending():
            body = path.read_text(encoding="utf-8")
            decode_poll(body)  # corrupted local outbox remains for inspection
            response = client.send_message(QueueUrl=queue_url, MessageBody=body)
            if not response.get("MessageId"):
                raise RuntimeError("SQS did not acknowledge SendMessage")
            path.unlink()  # sender can safely retry duplicates; observations are idempotent
            sent += 1
        return sent


def persist_poll(db_path: str | Path, body: str, *, now: float | None = None) -> tuple[dict, dict]:
    payload, observations = decode_poll(body)
    processed_at = time.time() if now is None else now
    store = Store(db_path)
    stats = store.record(observations, payload["source_errors"], payload["source_status"], at=processed_at)
    cf = payload.get("cloudfront_distribution")
    recent = store.events_since(processed_at - 2100, end=processed_at + AMP_CLOCK_SKEW_SEC)
    history = store.latest_cloudfront_observations(cf, at=processed_at) if cf else []
    snapshot = make_snapshot(recent, processed_at, errors=payload["source_errors"],
                             cloudfront_distribution=cf, cloudfront_last_observations=history)
    store.store_snapshot(snapshot)
    store.prune(retain_days=7, now=processed_at)
    return snapshot, stats


def receive_one(client: Any, queue_url: str, db_path: str | Path, *, wait_seconds: int = 20) -> dict:
    """Poison messages are not deleted; SQS redrives to DLQ after maxReceiveCount."""
    if not 0 <= wait_seconds <= 20:
        raise ValueError("wait_seconds must be 0..20")
    response = client.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1,
                                       WaitTimeSeconds=wait_seconds, VisibilityTimeout=120)
    items = response.get("Messages", [])
    if not items:
        return {"received": 0, "processed": 0}
    message = items[0]
    snapshot, stats = persist_poll(db_path, message["Body"])
    client.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])
    return {"received": 1, "processed": 1, "snapshot_id": snapshot["snapshot_id"],
            "inserted": stats["inserted"], "duplicates": stats["duplicates"]}


def queue_health(client: Any, queue_url: str) -> dict:
    raw = client.get_queue_attributes(QueueUrl=queue_url, AttributeNames=[
        "ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible",
        "ApproximateNumberOfMessagesDelayed", "RedrivePolicy", "QueueArn"
    ])["Attributes"]
    return {key: raw.get(key) for key in
            ("QueueArn", "ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible",
             "ApproximateNumberOfMessagesDelayed", "RedrivePolicy")}
