"""Directory-backed stand-in for the SQS telemetry queue (local demo only).

Implements just the three calls the existing transport uses
(``send_message``, ``receive_message``, ``delete_message``) so the unchanged
``Outbox.flush`` and ``receive_one`` from ``adaptivenet.ingestion.sqs_transport``
run against it. At-least-once: a received message becomes visible again if it
is not deleted within its visibility timeout.
"""
from __future__ import annotations

import os
from pathlib import Path
import time
import uuid

QUEUE_URL = "local://adaptivenet-demo-telemetry"


class FileQueue:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        (self.path / "ready").mkdir(parents=True, exist_ok=True)
        (self.path / "inflight").mkdir(parents=True, exist_ok=True)

    def _check(self, url: str) -> None:
        if url != QUEUE_URL:
            raise ValueError("demo queue accepts only its own local URL")

    def send_message(self, *, QueueUrl: str, MessageBody: str) -> dict:
        self._check(QueueUrl)
        ident = f"{time.time_ns():020d}-{uuid.uuid4().hex}"
        temp = self.path / f".{ident}.tmp"
        temp.write_text(MessageBody, encoding="utf-8")
        os.replace(temp, self.path / "ready" / f"{ident}.json")
        return {"MessageId": ident}

    def _requeue_expired(self) -> None:
        now = time.time()
        for item in (self.path / "inflight").glob("*.json"):
            try:
                deadline = float(item.name.split("__", 1)[0])
            except ValueError:
                continue
            if deadline <= now:
                original = item.name.split("__", 1)[1]
                try:
                    os.replace(item, self.path / "ready" / original)
                except FileNotFoundError:
                    pass

    def receive_message(self, *, QueueUrl: str, MaxNumberOfMessages: int = 1,
                        WaitTimeSeconds: int = 0, VisibilityTimeout: int = 120) -> dict:
        self._check(QueueUrl)
        deadline = time.monotonic() + WaitTimeSeconds
        while True:
            self._requeue_expired()
            for item in sorted((self.path / "ready").glob("*.json")):
                receipt = f"{time.time() + VisibilityTimeout:.3f}__{item.name}"
                try:
                    os.replace(item, self.path / "inflight" / receipt)
                except FileNotFoundError:
                    continue  # another consumer took it
                body = (self.path / "inflight" / receipt).read_text(encoding="utf-8")
                return {"Messages": [{"MessageId": item.stem, "ReceiptHandle": receipt, "Body": body}]}
            if time.monotonic() >= deadline:
                return {"Messages": []}
            time.sleep(0.25)

    def delete_message(self, *, QueueUrl: str, ReceiptHandle: str) -> dict:
        self._check(QueueUrl)
        if "/" in ReceiptHandle or ".." in ReceiptHandle:
            raise ValueError("invalid receipt handle")
        try:
            (self.path / "inflight" / ReceiptHandle).unlink()
        except FileNotFoundError:
            pass
        return {}

    def depth(self) -> dict:
        return {"ready": len(list((self.path / "ready").glob("*.json"))),
                "inflight": len(list((self.path / "inflight").glob("*.json")))}
