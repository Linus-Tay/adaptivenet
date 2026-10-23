"""Regression for research dataset safety and local operator permissions."""
from pathlib import Path
from adaptivenet.ingestion.models import Observation
from adaptivenet.ingestion.features import make_snapshot
from adaptivenet.ingestion.store import Store
from scripts.export_ml_dataset import export_snapshots

ROOT=Path(__file__).resolve().parents[1]
NOW=1800000000.0
CF="E1234567890ABC"

def test_ml_export_has_real_timestamp_and_missing_masks_without_stale_display_values(tmp_path):
    db=tmp_path/"test.db"
    store=Store(db)
    events=[Observation("global","aws_cloudwatch","cloudfront_requests",7,"requests",
                        NOW-30,NOW-1,CF)]
    store.record(events, [], "ok", at=NOW)
    snapshot=make_snapshot(events,NOW,cloudfront_distribution=CF,
                           cloudfront_last_observations=events)
    store.store_snapshot(snapshot)
    data=export_snapshots(db,mode="combined")
    assert len(data)==1
    row=data[0]
    assert row["snapshot_id"]==snapshot["snapshot_id"]
    assert row["computed_at_unix"]==NOW
    assert row["fault_label"] is None and row["label_source"] is None
    assert row["cloudfront_live_at_collection"]["features"]["cloudfront_requests"]==7
    assert "last_known" not in row["cloudfront_live_at_collection"]
    assert row["missing"]["region-a"]["app_up"] is True

def test_operator_requires_explicit_localhost_optin_and_confirm():
    server=(ROOT/"apps/dashboard/app/api/aws-fault/route.ts").read_text()
    assert 'ENABLE_REAL_AWS_FAULT_UI' in server
    assert '127.0.0.1:3000' in server
    assert 'STOP-REGION-A' in server and 'STOP-REGION-B' in server
    assert "90_000" in server
    assert '"--execute"' in server
    assert 'shell: false' in server
    assert 'lastSubmittedAt' in server

def test_fault_controls_are_separate_from_public_workload():
    ui=(ROOT/"apps/dashboard/app/page.tsx").read_text()
    workload=(ROOT/"apps/demo-web/app/page.tsx").read_text()
    assert "/api/aws-fault" in ui and 'function Info(' in ui
    assert "window.confirm(" in ui
    assert "/api/aws-fault" not in workload
    assert 'className="catalogue"' in workload and workload.count('className="timeline"')==1
    assert "+item.price.toFixed(2))" not in workload
