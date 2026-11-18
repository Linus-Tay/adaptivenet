"""SQS transport invariants: at-least-once, durability, no false ACK or timestamps."""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from adaptivenet.ingestion.models import Observation
from adaptivenet.ingestion.store import Store
from adaptivenet.ingestion.sqs_transport import (
    CONTRACT, MAX_MESSAGE_BYTES, Outbox, encode_poll, decode_poll,
    persist_poll, receive_one, resolve_queue_url,
)
from scripts import live_monitor

NOW = time.time()
QUEUE = "https://sqs.ap-southeast-1.amazonaws.com/468354413374/adaptivenet-dev-telemetry"


def observation():
    return Observation("region-a", "aws_amp", "cpu_busy_pct", 12.0, "%",
                       NOW - 10, NOW, "amp-test", {"job": "test"})


def envelope():
    return encode_poll([observation()], [], "ok", None, NOW)


class FakeSQS:
    def __init__(self):
        self.messages = []
        self.sent = []
        self.deleted = []
        self.fail_send = False

    def get_queue_url(self, **kwargs):
        assert kwargs["QueueName"] == "adaptivenet-dev-telemetry"
        return {"QueueUrl": QUEUE}

    def send_message(self, **kwargs):
        if self.fail_send:
            raise ConnectionError("intermittent transport failure")
        assert kwargs["QueueUrl"] == QUEUE
        self.sent.append(kwargs["MessageBody"])
        return {"MessageId": "msg-id"}

    def receive_message(self, **kwargs):
        assert kwargs["VisibilityTimeout"] == 120
        assert kwargs["WaitTimeSeconds"] <= 20
        return {"Messages": [self.messages.pop(0)]} if self.messages else {}

    def delete_message(self, **kwargs):
        self.deleted.append(kwargs["ReceiptHandle"])


def test_poll_contract_roundtrip_preserves_provenance():
    body = envelope()
    payload, rows = decode_poll(body)
    assert payload["contract_version"] == CONTRACT
    assert payload["batch_id"] and len(payload["batch_id"]) == 64
    assert rows == [observation()]
    assert rows[0].event_id == observation().event_id
    assert rows[0].observed_at == NOW - 10
    assert rows[0].collected_at == NOW


def test_invalid_and_tampered_payload_is_rejected():
    payload = json.loads(envelope())
    payload["observations"][0]["value"] = 999
    with pytest.raises(ValueError, match="checksum"):
        decode_poll(json.dumps(payload))
    with pytest.raises(ValueError):
        decode_poll("not json")
    with pytest.raises(ValueError, match="Oversized"):
        decode_poll("x" * (MAX_MESSAGE_BYTES+1))


def test_durable_outbox_replays_after_failed_send(tmp_path):
    outbox = Outbox(tmp_path / "outbox")
    body = envelope()
    stage = outbox.stage(body)
    assert stage.exists()
    assert len(outbox.pending()) == 1
    client = FakeSQS()
    client.fail_send = True
    with pytest.raises(ConnectionError):
        outbox.flush(client, QUEUE)
    assert stage.exists(), "Must never discard a poll before SQS confirms receipt"
    client.fail_send = False
    assert outbox.flush(client, QUEUE) == 1
    assert client.sent == [body]
    assert outbox.pending() == []


def test_receipt_deleted_only_after_sqlite_events_and_snapshot(tmp_path):
    client = FakeSQS()
    body = envelope()
    client.messages = [{"Body": body, "ReceiptHandle": "receipt-1"}]
    db = tmp_path / "telemetry.db"
    res = receive_one(client, QUEUE, db, wait_seconds=0)
    assert res["inserted"] == 1
    assert client.deleted == ["receipt-1"]
    assert Store(db).latest_snapshot()["snapshot_id"] == res["snapshot_id"]
    assert Store(db).events_since(NOW - 100, end=NOW + 100)[0].event_id == observation().event_id


def test_at_least_once_delivery_is_idempotent(tmp_path):
    client = FakeSQS()
    body = envelope()
    client.messages = [{"Body": body, "ReceiptHandle": "receipt-1"},
                       {"Body": body, "ReceiptHandle": "receipt-2"}]
    db = tmp_path / "telemetry.db"
    one = receive_one(client, QUEUE, db, wait_seconds=0)
    two = receive_one(client, QUEUE, db, wait_seconds=0)
    assert one["inserted"] == 1
    assert two["inserted"] == 0
    assert two["duplicates"] == 1
    assert client.deleted == ["receipt-1", "receipt-2"]


def test_failed_persistence_or_poison_message_never_acknowledged(tmp_path):
    client = FakeSQS()
    client.messages = [{"Body": "invalid-json", "ReceiptHandle": "bad"}]
    with pytest.raises(ValueError):
        receive_one(client, QUEUE, tmp_path / "db.sqlite", wait_seconds=0)
    assert client.deleted == []
    client.messages = [{"Body": envelope(), "ReceiptHandle": "good"}]
    with patch("adaptivenet.ingestion.sqs_transport.Store.record", side_effect=RuntimeError("disk full")):
        with pytest.raises(RuntimeError, match="disk full"):
            receive_one(client, QUEUE, tmp_path / "db.sqlite", wait_seconds=0)
    assert client.deleted == []


def test_queue_url_cannot_point_to_another_account_region_or_queue():
    client = FakeSQS()
    assert resolve_queue_url(client, account_id="468354413374") == QUEUE
    for bad in [
        "https://sqs.us-east-1.amazonaws.com/468354413374/adaptivenet-dev-telemetry",
        "https://sqs.ap-southeast-1.amazonaws.com/468354413374/other-queue",
        "https://sqs.ap-southeast-1.amazonaws.com/000000000000/adaptivenet-dev-telemetry",
    ]:
        with pytest.raises(ValueError):
            resolve_queue_url(client, bad, account_id="468354413374")


def test_monitor_switches_to_queue_without_running_direct_collector():
    cmd = live_monitor.command_set(profile="test", interval=120, traffic=False,
                                   probe_interval=60, sqs=True)
    joined = [" ".join(x[0]) for x in cmd]
    assert len(cmd) == 3
    assert any("publish-watch" in x for x in joined)
    assert any("consume-watch" in x for x in joined)
    assert not any("ingestion.py watch" in x for x in joined)


def test_terraform_uses_existing_queue_and_dlq(tmp_path):
    root = Path(__file__).resolve().parents[1]
    cfg = (root / "infra/terraform/environments/dev/main.tf").read_text(encoding="utf-8")
    permissions = (root / "infra/terraform/environments/dev/telemetry_iam_and_alarms.tf").read_text(encoding="utf-8")
    assert 'name                       = "adaptivenet-dev-telemetry"' in cfg
    assert "maxReceiveCount = 5" in cfg
    assert "sqs:SendMessage" in permissions and "sqs:ReceiveMessage" in permissions
    assert "ApproximateNumberOfMessagesVisible" in permissions

def test_queue_references_can_be_loaded_from_env_without_secrets(tmp_path):
    from adaptivenet.ingestion.config import read_env_file
    path = tmp_path / ".env"
    path.write_text(
        "ADAPTIVENET_SQS_QUEUE_URL=" + QUEUE + "\n" +
        "ADAPTIVENET_AWS_ACCOUNT_ID=468354413374\n" +
        "ATLAS_API_KEY=SHOULD_NOT_BE_READ\n",
        encoding="utf-8")
    result = read_env_file(path)
    assert result["ADAPTIVENET_SQS_QUEUE_URL"] == QUEUE
    assert result["ADAPTIVENET_AWS_ACCOUNT_ID"] == "468354413374"
    assert "ATLAS_API_KEY" not in result
