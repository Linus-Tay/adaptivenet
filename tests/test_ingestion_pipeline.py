"""Offline contract tests. Never contact AWS or RIPE and never change routing."""
from __future__ import annotations
import json
import math
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Works both in the overlay source tree and after being installed into AdaptiveNet.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "services"))
from adaptivenet.ingestion.models import Observation
from adaptivenet.ingestion.store import Store
from adaptivenet.ingestion.sources import parse_prometheus, parse_atlas_latest, PROMQL
from adaptivenet.ingestion.config import RegionConfig
from adaptivenet.ingestion.features import make_snapshot
from adaptivenet.ingestion.decisions import review_proposal

NOW = 1_800_000_000.0

def o(region, source, metric, value, ts=NOW, source_id="one", unit=None):
    return Observation(region=region, source=source, metric=metric, value=value,
        unit=unit or ("%" if metric.endswith("pct") else "unit"),
        observed_at=ts, collected_at=NOW, source_id=source_id)


def region_events(region, time=NOW):
    result = [o(region,"aws_amp","app_up",1,time),
              o(region,"aws_amp","memory_available_bytes",8e8,time),
              o(region,"aws_amp","memory_total_bytes",1e9,time),
              o(region,"aws_amp","cpu_busy_pct",35,time),
              o(region,"aws_amp","http_rps",2,time),
              o(region,"aws_amp","http_5xx_rps",0,time),
              o(region,"aws_amp","http_p95_ms",120,time)]
    for probe_id in range(1,6):
        sid=f"123/{probe_id}"
        result += [o(region,"ripe_atlas","packet_loss_pct",0,time,sid),
                   o(region,"ripe_atlas","network_rtt_ms",50+probe_id,time,sid)]
    return result


def test_observation_is_immutable_and_event_key_idempotent():
    a=o("region-a","ripe_atlas","network_rtt_ms",42,source_id="123/1")
    b=Observation(a.region,a.source,a.metric,a.value,a.unit,a.observed_at,NOW+20,a.source_id)
    assert a.event_id == b.event_id
    assert a.event_id != o("region-a","ripe_atlas","network_rtt_ms",43,source_id="123/1").event_id
    try:
        a.value=99
        assert False, 'Dataclass must be immutable'
    except AttributeError: pass


def test_reject_nan_and_future():
    with pytest.raises(ValueError): o("region-a","aws_amp","app_up",math.nan)
    with pytest.raises(ValueError): o("region-a","aws_amp","app_up",1,NOW+600)


def test_wal_journal_deduplicates_and_persists(tmp_path):
    st=Store(tmp_path/'journal.db')
    events=region_events('region-a')
    first=st.record(events, [], 'ok', at=NOW)
    again=st.record(events, [], 'ok', at=NOW+10)
    assert first['inserted']==len(events)
    assert again['inserted']==0 and again['duplicates']==len(events)
    assert len(st.events_since(NOW-10,end=NOW+10))==len(events)
    assert st.latest_run()['duplicates']==len(events)
    assert (tmp_path/'journal.db').exists()


def test_expiry_keeps_missing_values_null_and_blocks_model_readiness():
    events=region_events('region-a', NOW-3600)+region_events('region-b',NOW-3600)
    snap=make_snapshot(events,NOW)
    assert snap['regions']['region-a']['features']['app_up'] is None
    assert snap['regions']['region-a']['features']['packet_loss_pct'] is None
    assert snap['ready_for_model'] is False
    assert snap['ready_for_automatic_action'] is False


def test_feature_fusion_comparison_and_provenance():
    aa=region_events('region-a')
    bb=region_events('region-b')
    # Increase region-b RTT by 80ms per probe
    bb=[o(v.region,v.source,v.metric,v.value+80 if v.metric=='network_rtt_ms' else v.value,
          v.observed_at,v.source_id,v.unit) for v in bb]
    snap=make_snapshot(aa+bb,NOW)
    assert snap['ready_for_model'] is True
    assert snap['regions']['region-a']['features']['network_rtt_p50_ms']==53
    assert snap['comparison']['rtt_p50_a_minus_b_ms']==-80
    assert snap['regions']['region-a']['features']['memory_available_pct']==80
    assert snap['regions']['region-a']['features']['http_error_ratio']==0
    assert snap['regions']['region-a']['provenance']['app_up']['source']=='aws_amp'


def test_atlas_probe_dedupe_and_loss_only():
    cfg=RegionConfig('region-a','ap-southeast-1','ws-x',123)
    payload={'42':[{'prb_id':42,'timestamp':NOW-5,'sent':3,'result':[{'rtt':25.5},{'x':'timeout'},{'rtt':34.5}]}],
             '43':[{'prb_id':43,'timestamp':NOW-5,'sent':3,'result':[{}, {}, {}]}]}
    events=parse_atlas_latest(payload,cfg,NOW)
    loss=sorted(v.value for v in events if v.metric=='packet_loss_pct')
    assert loss==[round(100/3,10),100.0] or all(abs(a-b)<.01 for a,b in zip(loss,[100/3,100]))
    assert len([v for v in events if v.metric=='network_rtt_ms'])==1
    assert events[0].source_id.startswith('123/')


def test_prometheus_stale_and_missing_series_are_not_zero():
    payload={'status':'success','data':{'resultType':'vector','result':[{'value':[NOW-1000,'1']} ]}}
    assert parse_prometheus(payload,NOW) is None
    payload['data']['result'] = []
    assert parse_prometheus(payload,NOW) is None
    payload['data']['result'] = [{'value':[NOW-1,'NaN']}]
    assert parse_prometheus(payload,NOW) is None


def test_promql_filters_region_and_app_identity():
    for name in ('http_rps','http_5xx_rps','http_p95_ms','app_up'):
        assert 'region="{name}"' in PROMQL[name][0]
        assert 'project="adaptivenet"' in PROMQL[name][0]


def test_decision_is_never_executable_and_stale_snapshot_denied():
    snap=make_snapshot(region_events('region-a')+region_events('region-b'),NOW)
    proposal={'contract_version':'adaptivenet.proposal.v1','snapshot_id':snap['snapshot_id'],
              'action':'shift_traffic','confidence':.99,'target_region_a_percent':40,
              'evidence':['region-a:rtt_p95']}
    with patch('adaptivenet.ingestion.decisions.time.time', return_value=NOW+30):
        review=review_proposal(snap,proposal,current_region_a_percent=50)
    assert review['eligible_for_operator_review'] is True
    assert review['approved_for_execution'] is False
    assert review['executed'] is False
    with patch('adaptivenet.ingestion.decisions.time.time', return_value=NOW+500):
        expired=review_proposal(snap,proposal,current_region_a_percent=50)
    assert 'feature_snapshot_expired_or_invalid' in expired['rejection_reasons']
    with patch('adaptivenet.ingestion.decisions.time.time', return_value=NOW+30):
        proposal['target_region_a_percent']=0
        big=review_proposal(snap,proposal,current_region_a_percent=50)
    assert 'route_delta_above_25_percent_safety_limit' in big['rejection_reasons']


@pytest.mark.parametrize(("current_weight", "metric", "value", "reason"), [
    ("50", "memory_available_pct", 80, "invalid_current_route_weight"),
    (True, "memory_available_pct", 80, "invalid_current_route_weight"),
    (50, "memory_available_pct", math.nan, "destination_low_or_unknown_memory"),
    (50, "memory_available_pct", math.inf, "destination_low_or_unknown_memory"),
    (50, "memory_available_pct", "80", "destination_low_or_unknown_memory"),
    (50, "cpu_busy_pct", math.nan, "destination_high_or_unknown_cpu"),
    (50, "cpu_busy_pct", False, "destination_high_or_unknown_cpu"),
    (50, "cpu_busy_pct", -1, "destination_high_or_unknown_cpu"),
    (50, "cpu_busy_pct", 85, "destination_high_or_unknown_cpu"),
])
def test_decision_rejects_invalid_route_weight_or_unsafe_destination_capacity(
    current_weight, metric, value, reason,
):
    snapshot = make_snapshot(region_events("region-a") + region_events("region-b"), NOW)
    snapshot["regions"]["region-b"]["features"][metric] = value
    proposal = {
        "contract_version": "adaptivenet.proposal.v1",
        "snapshot_id": snapshot["snapshot_id"],
        "action": "shift_traffic",
        "confidence": 0.99,
        "target_region_a_percent": 40,
        "evidence": ["region-a:rtt_p95"],
    }
    with patch("adaptivenet.ingestion.decisions.time.time", return_value=NOW + 30):
        review = review_proposal(snapshot, proposal, current_region_a_percent=current_weight)

    assert review["rejection_reasons"] == [reason]
    assert review["policy_valid"] is False
    assert review["eligible_for_operator_review"] is False
    assert review["approved_for_execution"] is False
    assert review["executed"] is False
    assert review["effective_action"] == "hold"


def test_feature_snapshot_persistence_and_retention(tmp_path):
    st=Store(tmp_path/'features.db')
    snap=make_snapshot(region_events('region-a')+region_events('region-b'),NOW)
    st.store_snapshot(snap)
    st.store_snapshot(snap)
    assert st.latest_snapshot()['snapshot_id']==snap['snapshot_id']
    assert len(st.recent_snapshots())==1
    st.record(region_events('region-a'),[],'ok',at=NOW)
    st.prune(retain_days=1,now=NOW+86400*2)
    assert st.latest_snapshot() is None
    assert not st.events_since(NOW-3600,end=NOW+86400*2)
