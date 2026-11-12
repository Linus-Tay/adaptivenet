"""Demo collector: lab measurements -> durable outbox -> local queue.

Mirrors the real pipeline shape (collector -> outbox -> SQS -> feature worker)
using the unchanged ``encode_poll`` / ``Outbox`` code. The only swap is the
transport: a directory queue instead of SQS, so no AWS access is needed.

Three measurement streams, all real measurements of the local lab:
* internal (AMP stand-in): origin ``/lab/metrics`` scraped on the internal network;
* external (RIPE stand-in): several lab probes ping each region via the edge path;
* HTTP witness: one 5-second request per scope via the edge path, appended to
  the same ``probe-events.jsonl`` journal format the native witness writes.

Observations reuse the existing ``aws_amp`` / ``ripe_atlas`` source names because
the feature contract only accepts those; every one carries a ``lab`` label so the
emulated origin is never mistaken for real AWS or RIPE data.
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import time

import httpx

from adaptivenet.ingestion.models import Observation, REGIONS
from adaptivenet.ingestion.sqs_transport import Outbox, encode_poll
from adaptivenet.ingestion.witness import append_probe

from .file_queue import FileQueue, QUEUE_URL

log = logging.getLogger("demo.collector")
INTERNAL_METRICS = (("app_up", "bool"), ("cpu_busy_pct", "percent"),
                    ("memory_available_bytes", "bytes"), ("memory_total_bytes", "bytes"),
                    ("node_load1", "load"), ("http_rps", "rps"), ("http_5xx_rps", "rps"),
                    ("http_p95_ms", "ms"))


def internal_observations(region: str, body: dict, collected_at: float) -> list[Observation]:
    """Turn one origin scrape into observations. Null values are skipped, not zeroed."""
    observed = body.get("observed_at")
    if type(observed) not in (int, float):
        return []
    out = []
    for metric, unit in INTERNAL_METRICS:
        value = body.get(metric)
        if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        out.append(Observation(region, "aws_amp", metric, float(value), unit, float(observed),
                               collected_at, f"lab-origin-{region}", {"lab": "local_origin"}))
    return out


def probe_observations(region: str, probe_id: str, replies: list[float | None],
                       observed_at: float, collected_at: float) -> list[Observation]:
    """One RIPE-style result per probe: loss over N pings, median RTT of replies."""
    if not replies:
        return []
    lost = sum(1 for r in replies if r is None)
    labels = {"lab": "edge_emulator"}
    out = [Observation(region, "ripe_atlas", "packet_loss_pct", round(100 * lost / len(replies), 3),
                       "percent", observed_at, collected_at, probe_id, labels)]
    rtts = sorted(r for r in replies if r is not None)
    if rtts:
        out.append(Observation(region, "ripe_atlas", "network_rtt_ms", round(rtts[len(rtts) // 2], 3),
                               "ms", observed_at, collected_at, probe_id, labels))
    return out


class Collector:
    def __init__(self, *, data_dir: Path, client: httpx.Client, origins: dict[str, str],
                 edge_url: str, probes: int = 5, pings: int = 3):
        self.data_dir = data_dir
        self.client = client
        self.origins = origins
        self.edge = edge_url.rstrip("/")
        self.probes = probes
        self.pings = pings
        self.outbox = Outbox(data_dir / "outbox")
        self.queue = FileQueue(data_dir / "queue")
        self.witness = data_dir / "probe-events.jsonl"

    def witness_tick(self) -> None:
        for scope in ("cloudfront", "region-a", "region-b"):
            started = time.time()
            event = {"timestamp": datetime.fromtimestamp(started, timezone.utc).isoformat(),
                     "scope": scope, "kind": "lab_witness"}
            try:
                response = self.client.get(f"{self.edge}/http/{scope}", timeout=3)
                latency = round((time.time() - started) * 1000, 2)
                body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
                served = body.get("region") if isinstance(body, dict) else None
                event.update(http_status=response.status_code, latency_ms=latency, origin=served,
                             outcome="ok" if response.status_code == 200 and served in REGIONS
                             else "http_error" if response.status_code >= 400 else "invalid_response")
                if scope != "cloudfront" and event["outcome"] == "ok" and served != scope:
                    event["outcome"] = "unexpected_region"
            except httpx.HTTPError:
                event.update(outcome="network_error", latency_ms=None)
            append_probe(self.witness, event)

    def internal(self, collected_at: float) -> tuple[list[Observation], list[str]]:
        events, errors = [], []
        for region, url in self.origins.items():
            try:
                response = self.client.get(url + "/lab/metrics", timeout=3)
                response.raise_for_status()
                events += internal_observations(region, response.json(), collected_at)
            except (httpx.HTTPError, ValueError):
                errors.append(f"{region}:lab_origin_metrics:fetch_failed")
        return events, errors

    def external(self, collected_at: float) -> tuple[list[Observation], list[str]]:
        events, errors = [], []
        for region in REGIONS:
            for n in range(1, self.probes + 1):
                replies: list[float | None] = []
                observed = time.time()
                for _ in range(self.pings):
                    started = time.perf_counter()
                    try:
                        response = self.client.get(f"{self.edge}/ping/{region}", timeout=2)
                        replies.append(round((time.perf_counter() - started) * 1000, 3)
                                       if response.status_code == 200 else None)
                    except httpx.HTTPError:
                        replies.append(None)
                if not replies:
                    errors.append(f"{region}:lab_probe_{n}:no_result")
                events += probe_observations(region, f"lab-probe-{n}", replies, observed, time.time())
        return events, errors

    def publish(self, events: list[Observation], errors: list[str], collected_at: float) -> None:
        status = "ok" if not errors else "partial"
        body = encode_poll(events, errors, status, None, collected_at)
        self.outbox.stage(body)          # durable before any send
        sent = self.outbox.flush(self.queue, QUEUE_URL)
        log.info("published poll: %d observations, %d errors, %d sent", len(events), len(errors), sent)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    data = Path(os.environ.get("DEMO_DATA_DIR", "/app/data"))
    witness_every = float(os.environ.get("WITNESS_INTERVAL_SEC", "5"))
    internal_every = float(os.environ.get("INTERNAL_INTERVAL_SEC", "15"))
    external_every = float(os.environ.get("EXTERNAL_INTERVAL_SEC", "30"))
    with httpx.Client(trust_env=False) as client:
        collector = Collector(
            data_dir=data, client=client, edge_url=os.environ["EDGE_URL"],
            origins={"region-a": os.environ["REGION_A_URL"], "region-b": os.environ["REGION_B_URL"]},
            probes=int(os.environ.get("LAB_PROBES", "5")))
        next_internal = next_external = 0.0
        while True:
            tick = time.monotonic()
            collector.witness_tick()
            events, errors = [], []
            now = time.time()
            if tick >= next_internal:
                e, err = collector.internal(now); events += e; errors += err
                next_internal = tick + internal_every
            if tick >= next_external:
                e, err = collector.external(now); events += e; errors += err
                next_external = tick + external_every
            if events or errors:
                try:
                    collector.publish(events, errors, time.time())
                except (RuntimeError, ValueError, OSError) as exc:
                    log.warning("publish failed, poll stays in outbox: %s", exc)
            time.sleep(max(0.0, witness_every - (time.monotonic() - tick)))


if __name__ == "__main__":
    main()
