"""Bounded, read-only experiment witness journal. Never used for ML readiness."""
from __future__ import annotations
import json
import os
from pathlib import Path

MAX_HISTORY_BYTES = 512 * 1024

def append_probe(path: str | Path, event: dict) -> None:
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    allowed = ("timestamp", "scope", "http_status", "latency_ms", "origin", "outcome", "kind", "duration_seconds")
    line = json.dumps({k: event[k] for k in allowed if k in event}, separators=(",", ":"), allow_nan=False) + "\n"
    if file.exists() and file.stat().st_size > MAX_HISTORY_BYTES:
        tail = file.read_bytes()[-MAX_HISTORY_BYTES // 2:]
        tail = tail.partition(b"\n")[2]
        temp = file.with_name(file.name + ".rotating")
        temp.write_bytes(tail)
        os.replace(temp, file)
    with file.open("a", encoding="utf-8") as stream:
        stream.write(line)

def recent_probes(path: str | Path, limit: int = 30) -> list[dict]:
    limit = max(1, min(1000, int(limit)))
    file = Path(path)
    if not file.exists():
        return []
    try:
        rows = file.read_text(encoding="utf-8").splitlines()[-limit:]
    except (OSError, UnicodeError):
        return []
    out = []
    for row in reversed(rows):
        try:
            obj = json.loads(row)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and obj.get("scope") in ("cloudfront", "region-a", "region-b"):
            out.append(obj)
    return out
