"""Append-only JSONL audit trail. Also the source of truth for cooldown/rate limits,
so restarting the controller cannot reset safety state."""
from __future__ import annotations
import json
import os
from pathlib import Path
import threading
import time

class AuditLog:
    def __init__(self, path: str | Path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, event: str, **fields) -> dict:
        record = {"ts": self.clock(), "event": event, **fields}
        line = (json.dumps(record, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
        with self._lock, open(self.path, "a+b") as fh:
            # A crash mid-write leaves a partial line; start on a fresh one so this record survives.
            if fh.seek(0, os.SEEK_END):
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    line = b"\n" + line
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        return record

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue  # a torn line must not disable the controller
            if isinstance(record, dict) and isinstance(record.get("ts"), (int, float)):
                out.append(record)
        return out

    def actions_since(self, start: float) -> list[dict]:
        """Actions that actually touched infrastructure (including failed attempts)."""
        return [r for r in self.read() if r.get("event") == "step_applied" and r["ts"] >= start]
