from concurrent.futures import ThreadPoolExecutor
import math
import sqlite3
from threading import Barrier

import pytest

from adaptivenet.controller.journal import ActionJournal, PendingAction
from adaptivenet.controller.reconcile import reconcile_route


def test_pending_action_survives_restart_and_rejects_duplicate(tmp_path, monkeypatch):
    path = tmp_path / "controller.db"
    monkeypatch.setattr("adaptivenet.controller.journal.time.time", lambda: 100)
    journal = ActionJournal(path)
    assert journal.pending() is None
    assert journal.record_pending(
        action_id="action-1", intent="shift:region-b:40", before={"region_a_percent": 50},
        expected={"region_a_percent": 40},
    )

    restarted = ActionJournal(path)
    assert not restarted.record_pending(
        action_id="action-1", intent="shift:region-b:40", before={"region_a_percent": 45},
        expected={"region_a_percent": 35},
    )
    assert restarted.pending() == PendingAction(
        "action-1", "shift:region-b:40", {"region_a_percent": 50}, 100, {"region_a_percent": 40},
    )


def test_only_one_competing_writer_reserves_pending_slot(tmp_path):
    journal = ActionJournal(tmp_path / "controller.db")
    start = Barrier(2, timeout=5)

    def reserve(action_id):
        start.wait()
        return journal.record_pending(
            action_id=action_id, intent="shift:region-b:40", before={"region_a_percent": 50},
            expected={"region_a_percent": 40},
        )

    action_ids = ["action-1", "action-2"]
    with ThreadPoolExecutor(max_workers=2) as executor:
        accepted = list(executor.map(reserve, action_ids))

    assert accepted.count(True) == 1
    assert journal.pending().action_id == action_ids[accepted.index(True)]


def test_unserializable_before_state_does_not_reserve_slot(tmp_path):
    journal = ActionJournal(tmp_path / "controller.db")
    with pytest.raises(ValueError):
        journal.record_pending(
            action_id="action-1", intent="shift:region-b:40", before={"region_a_percent": math.nan},
            expected={"region_a_percent": 40},
        )
    assert journal.pending() is None


def test_storage_failure_cannot_report_a_successful_reservation(tmp_path):
    path = tmp_path / "controller.db"
    journal = ActionJournal(path)
    path.unlink()
    with pytest.raises(sqlite3.OperationalError):
        journal.record_pending(
            action_id="action-1", intent="shift:region-b:40", before={"region_a_percent": 50},
            expected={"region_a_percent": 40},
        )


@pytest.mark.parametrize(("observed", "outcome"), [
    (40, "target_observed"),
    (50, "original_observed"),
    (30, "unexpected_route"),
    (None, "unknown"),
    ("40", "unknown"),
    (True, "unknown"),
])
def test_reconcile_reads_saved_target_and_keeps_action_pending(tmp_path, observed, outcome):
    path = tmp_path / "controller.db"
    journal = ActionJournal(path)
    assert journal.record_pending(
        action_id="action-1", intent="shift:region-b:40", before={"region_a_percent": 50},
        expected={"region_a_percent": 40},
    )
    restarted = ActionJournal(path)
    pending = restarted.pending()
    assert reconcile_route(pending, observed_region_a_percent=observed) == outcome
    assert restarted.pending() == pending


def test_legacy_pending_row_is_preserved_without_guessing_target(tmp_path):
    path = tmp_path / "controller.db"
    with sqlite3.connect(path) as db:
        db.execute("""
            CREATE TABLE pending_action (
                slot INTEGER PRIMARY KEY, action_id TEXT, intent TEXT,
                before_json TEXT, recorded_at REAL
            )
        """)
        db.execute(
            "INSERT INTO pending_action VALUES (1, ?, ?, ?, 100)",
            ("action-1", "shift:region-b:40", '{"region_a_percent": 50}'),
        )
    pending = ActionJournal(path).pending()
    assert pending == PendingAction("action-1", "shift:region-b:40", {"region_a_percent": 50}, 100)
    assert reconcile_route(pending, observed_region_a_percent=40) == "unknown"


def test_resolved_action_id_cannot_be_reserved_or_resolved_twice(tmp_path):
    journal = ActionJournal(tmp_path / "controller.db")
    shift = {
        "action_id": "action-1", "intent": "shift:100->75",
        "before": {"region_a_percent": 100}, "expected": {"region_a_percent": 75},
    }
    assert journal.record_pending(**shift)
    assert journal.resolve("action-1", state="continue_candidate", reason="source_still_missing_deadline")
    assert not journal.resolve("action-1", state="recovered", reason="client_window_met_deadline")
    assert not journal.record_pending(**shift)
    assert journal.pending() is None
    assert journal.outcome("action-1").state == "continue_candidate"
