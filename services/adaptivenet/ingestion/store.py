"""SQLite WAL append-only-ish event journal + immutable feature snapshots.

Event IDs guarantee repeat polling of a cached RIPE result is idempotent.
Reads/writes each acquire their own connection, suitable for a local API thread.
"""
from __future__ import annotations
from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3
import time
from .models import Observation

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
  event_id TEXT PRIMARY KEY,
  region TEXT NOT NULL,
  source TEXT NOT NULL,
  metric TEXT NOT NULL,
  value REAL NOT NULL,
  unit TEXT NOT NULL,
  observed_at REAL NOT NULL,
  collected_at REAL NOT NULL,
  source_id TEXT NOT NULL,
  labels_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_observation_window ON observations(region,metric,observed_at DESC);
CREATE TABLE IF NOT EXISTS ingestion_runs (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
  collected_at REAL NOT NULL,
  source_status TEXT NOT NULL,
  inserted INTEGER NOT NULL,
  duplicates INTEGER NOT NULL,
  errors_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feature_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  computed_at REAL NOT NULL,
  body_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_snapshots_computed ON feature_snapshots(computed_at DESC);
"""

class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        con = sqlite3.connect(str(self.path), timeout=15)
        try:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA busy_timeout=10000")
            con.execute("PRAGMA journal_mode=WAL")
            yield con
            con.commit()
        finally:
            con.close()

    def record(self, events: list[Observation], errors: list[str], status: str, at: float | None = None) -> dict:
        now = time.time() if at is None else at
        # Keep entire poll atomic: either events and run row commit together or none do.
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            inserted = 0
            for e in events:
                cur = con.execute("""INSERT OR IGNORE INTO observations
                    (event_id,region,source,metric,value,unit,observed_at,collected_at,source_id,labels_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (e.event_id, e.region, e.source, e.metric, e.value, e.unit,
                     e.observed_at, e.collected_at, e.source_id,
                     json.dumps(e.labels, sort_keys=True)))
                inserted += cur.rowcount
            con.execute("INSERT INTO ingestion_runs(collected_at,source_status,inserted,duplicates,errors_json) VALUES(?,?,?,?,?)",
                        (now, status, inserted, len(events)-inserted, json.dumps(errors)))
        return {"inserted": inserted, "duplicates": len(events)-inserted, "errors": errors}

    def events_since(self, start: float, *, end: float | None = None) -> list[Observation]:
        end = time.time() if end is None else end
        with self.connect() as con:
            rows = con.execute("""SELECT region,source,metric,value,unit,observed_at,collected_at,source_id,labels_json
                                  FROM observations WHERE observed_at BETWEEN ? AND ? ORDER BY observed_at ASC,event_id ASC""",
                               (start, end)).fetchall()
        return [Observation(r["region"], r["source"], r["metric"], r["value"], r["unit"],
                            r["observed_at"], r["collected_at"], r["source_id"], json.loads(r["labels_json"])) for r in rows]

    def store_snapshot(self, snapshot: dict):
        with self.connect() as con:
            con.execute("INSERT OR IGNORE INTO feature_snapshots(snapshot_id,computed_at,body_json) VALUES(?,?,?)",
                        (snapshot["snapshot_id"], snapshot["computed_at_unix"], json.dumps(snapshot, allow_nan=False)))

    def latest_snapshot(self) -> dict | None:
        with self.connect() as con:
            row = con.execute("SELECT body_json FROM feature_snapshots ORDER BY computed_at DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None

    def recent_snapshots(self, limit=50) -> list[dict]:
        limit = max(1, min(500, int(limit)))
        with self.connect() as con:
            rows = con.execute("SELECT body_json FROM feature_snapshots ORDER BY computed_at DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def latest_run(self) -> dict | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM ingestion_runs ORDER BY run_id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        return {"run_id": row["run_id"], "collected_at": row["collected_at"],
                "source_status": row["source_status"], "inserted": row["inserted"],
                "duplicates": row["duplicates"], "errors": json.loads(row["errors_json"])}

    def prune(self, *, retain_days=7, now: float | None = None):
        if retain_days < 1:
            raise ValueError("minimum retention is one day")
        now = time.time() if now is None else now
        cutoff = now - retain_days * 86400
        with self.connect() as con:
            con.execute("DELETE FROM observations WHERE observed_at < ?", (cutoff,))
            con.execute("DELETE FROM ingestion_runs WHERE collected_at < ?", (cutoff,))
            con.execute("DELETE FROM feature_snapshots WHERE computed_at < ?", (cutoff,))
