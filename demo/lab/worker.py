"""Demo feature worker: local queue -> unchanged ``receive_one`` -> SQLite snapshots."""
from __future__ import annotations

import logging
import os
from pathlib import Path
import time

from adaptivenet.ingestion.sqs_transport import receive_one

from .file_queue import FileQueue, QUEUE_URL

log = logging.getLogger("demo.worker")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    data = Path(os.environ.get("DEMO_DATA_DIR", "/app/data"))
    queue = FileQueue(data / "queue")
    db = data / "adaptivenet-ingestion.db"
    while True:
        try:
            result = receive_one(queue, QUEUE_URL, db, wait_seconds=10)
            if result["received"]:
                log.info("stored %s (+%d obs, %d dup)", result["snapshot_id"],
                         result["inserted"], result["duplicates"])
        except (ValueError, OSError) as exc:
            # Poison message: left in flight; it is retried after the visibility timeout.
            log.warning("message not processed: %s", exc)
            time.sleep(2)


if __name__ == "__main__":
    main()
