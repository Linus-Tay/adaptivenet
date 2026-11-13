"""Durable pre-action records for the independent controller."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import time


@dataclass(frozen=True)
class PendingAction:
    action_id: str
    intent: str
    before: dict
    recorded_at: float
    expected: dict | None = None


@dataclass(frozen=True)
class ActionOutcome:
    action_id: str
    state: str
    reason: str
    resolved_at: float
    original_weight: int | None
    target_weight: int | None
    intent: str


class ActionJournal:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                CREATE TABLE IF NOT EXISTS pending_action (
                    slot INTEGER PRIMARY KEY CHECK (slot = 1),
                    action_id TEXT NOT NULL,
                    intent TEXT NOT NULL,
                    before_json TEXT NOT NULL,
                    recorded_at REAL NOT NULL,
                    expected_json TEXT
                )
            """)
            columns = {column[1] for column in db.execute("PRAGMA table_info(pending_action)")}
            if "expected_json" not in columns:
                db.execute("ALTER TABLE pending_action ADD COLUMN expected_json TEXT")
            db.execute("""
                CREATE TABLE IF NOT EXISTS action_outcome (
                    sequence INTEGER PRIMARY KEY,
                    action_id TEXT NOT NULL UNIQUE,
                    intent TEXT NOT NULL,
                    before_json TEXT NOT NULL,
                    expected_json TEXT,
                    recorded_at REAL NOT NULL,
                    resolved_at REAL NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('blocked', 'recovered', 'continue_candidate')),
                    reason TEXT NOT NULL
                )
            """)

    def record_pending(self, *, action_id: str, intent: str, before: dict, expected: dict) -> bool:
        """Return True only when this call commits a new pending record.

        False means reconciliation is required, including for a repeated ID.
        Storage and serialization errors propagate to the caller.
        """
        before_json = json.dumps(before, allow_nan=False)
        expected_json = json.dumps(expected, allow_nan=False)
        with closing(sqlite3.connect(self.path)) as db, db:
            result = db.execute("""
                INSERT INTO pending_action
                    (slot, action_id, intent, before_json, recorded_at, expected_json)
                SELECT 1, ?, ?, ?, ?, ?
                WHERE NOT EXISTS (SELECT 1 FROM action_outcome WHERE action_id = ?)
                ON CONFLICT(slot) DO NOTHING
            """, (action_id, intent, before_json, time.time(), expected_json, action_id))
            return result.rowcount == 1

    def pending(self) -> PendingAction | None:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("""
                SELECT action_id, intent, before_json, recorded_at, expected_json
                FROM pending_action
            """).fetchone()
        if row is None:
            return None
        action_id, intent, before_json, recorded_at, expected_json = row
        expected = json.loads(expected_json) if expected_json is not None else None
        return PendingAction(action_id, intent, json.loads(before_json), recorded_at, expected)

    def resolve(self, action_id: str, *, state: str, reason: str) -> bool:
        """Record the decision once and release the slot only on an accepted outcome."""
        with closing(sqlite3.connect(self.path)) as db, db:
            result = db.execute("""
                INSERT INTO action_outcome
                    (action_id, intent, before_json, expected_json, recorded_at, resolved_at, state, reason)
                SELECT action_id, intent, before_json, expected_json, recorded_at, ?, ?, ?
                FROM pending_action WHERE action_id = ?
                ON CONFLICT(action_id) DO NOTHING
            """, (time.time(), state, reason, action_id))
            if result.rowcount != 1:
                return False
            if state != "blocked":
                db.execute("DELETE FROM pending_action WHERE action_id = ?", (action_id,))
            return True

    def outcome(self, action_id: str | None = None) -> ActionOutcome | None:
        query = """
            SELECT action_id, state, reason, resolved_at, before_json, expected_json, intent
            FROM action_outcome
        """
        with closing(sqlite3.connect(self.path)) as db:
            if action_id is None:
                row = db.execute(query + " ORDER BY sequence DESC LIMIT 1").fetchone()
            else:
                row = db.execute(query + " WHERE action_id = ?", (action_id,)).fetchone()
        if row is None:
            return None
        action_id, state, reason, resolved_at, before_json, expected_json, intent = row
        before = json.loads(before_json)
        expected = json.loads(expected_json) if expected_json is not None else {}
        return ActionOutcome(
            action_id, state, reason, resolved_at, before.get("region_a_percent"),
            expected.get("region_a_percent"), intent,
        )
