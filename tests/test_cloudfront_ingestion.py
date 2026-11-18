"""No-AWS tests: CloudFront dimensions, freshness, storage, dashboard, safety."""
from pathlib import Path
from unittest.mock import patch, Mock
from datetime import datetime, timezone
import json
import sys
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services'))
from adaptivenet.ingestion.cloudfront import (
    STANDARD, metric_queries, parse_metric_data, collect_cloudfront, distribution_id,
)
from adaptivenet.ingestion.models import Observation
from adaptivenet.ingestion.store import Store
from adaptivenet.ingestion.features import make_snapshot
from adaptivenet.ingestion.handoff import build_model_input
from adaptivenet.ingestion.api import HTML

NOW=1800000000.0
CF="E1234567890ABC"
def iso(t):return datetime.fromtimestamp(t,timezone.utc).isoformat()

def metric_result(key,ts,value):
    return {'Id':key,'Timestamps':[iso(ts)],'Values':[value],'StatusCode':'Complete'}

def test_dimensions_metrics_statistics():
    m=metric_queries(CF)
    assert len(m)==6
    lookup={q['MetricStat']['Metric']['MetricName']:q for q in m}
    assert lookup['Requests']['MetricStat']['Stat']=='Sum'
    assert lookup['4xxErrorRate']['MetricStat']['Stat']=='Average'
    assert lookup['5xxErrorRate']['MetricStat']['Stat']=='Average'
    for q in m:
        assert q['MetricStat']['Metric']['Namespace']=='AWS/CloudFront'
        assert q['MetricStat']['Metric']['Dimensions']==[
            {'Name':'DistributionId','Value':CF},{'Name':'Region','Value':'Global'}]
        assert q['MetricStat']['Period']==60


def test_cloudwatch_parser_real_timestamp_and_missing_values():
    raw={'MetricDataResults':[metric_result('m0',NOW-35,25),metric_result('m1',NOW-65,0),metric_result('m2',NOW-100,5.5)]}
    events=parse_metric_data(raw,CF,NOW)
    assert {e.metric:e.value for e in events}=={'cloudfront_requests':25.,'cloudfront_4xx_pct':0.,'cloudfront_5xx_pct':5.5}
    assert all(e.region=='global' and e.source=='aws_cloudwatch' for e in events)
    assert events[0].observed_at==NOW-35
    assert 'cloudfront_bytes_uploaded' not in {e.metric for e in events}


def test_cloudwatch_rejects_stale_future_nonfinite_and_bad_values():
    raw={'MetricDataResults':[
       metric_result('m0',NOW-10000,100),metric_result('m1',NOW-35,150),
       metric_result('m2',NOW+1000,3),metric_result('m3',NOW-50,float('nan'))]}
    assert parse_metric_data(raw,CF,NOW)==[]
    with pytest.raises(ValueError):parse_metric_data({},CF,NOW)
    with pytest.raises(ValueError):metric_queries('bad; DROP')


def test_valid_cloudfront_stays_global_and_deduplicates(tmp_path):
    ev=parse_metric_data({'MetricDataResults':[metric_result('m0',NOW-30,5)]},CF,NOW)
    store=Store(tmp_path/'storage.db')
    assert store.record(ev,[],'ok',at=NOW)['inserted']==1
    assert store.record(ev,[],'ok',at=NOW+2)['duplicates']==1
    snap=make_snapshot(store.events_since(NOW-60,end=NOW),NOW,cloudfront_distribution=CF)
    assert snap['cloudfront']['status']=='fresh'
    assert snap['cloudfront']['features']['cloudfront_requests']==5
    assert snap['cloudfront']['features']['cloudfront_4xx_pct'] is None
    assert snap['ready_for_model'] is False
    store.store_snapshot(snap)
    assert store.latest_snapshot()['cloudfront']['status']=='fresh'
    assert build_model_input(snap,mode='internal_only',now=NOW+10)['cloudfront']['features']['cloudfront_requests']==5


def test_missing_distribution_explicit_and_cf_optional():
    no=make_snapshot([],NOW)
    assert no['cloudfront']['status']=='not_configured'
    assert no['cloudfront']['distribution_id'] is None
    configured=make_snapshot([],NOW,cloudfront_distribution=CF)
    assert configured['cloudfront']['status']=='no_fresh_metrics'
    assert configured['ready_for_model']==no['ready_for_model']


def test_global_cloudfront_only_source_and_region_policy():
    with pytest.raises(ValueError):Observation('region-a','aws_cloudwatch','cloudfront_requests',1,'requests',NOW,NOW,CF)
    with pytest.raises(ValueError):Observation('global','aws_amp','app_up',1,'bool',NOW,NOW)


def test_env_precedence(tmp_path,monkeypatch):
    env=tmp_path/'.env';env.write_text('CLOUDFRONT_DISTRIBUTION_ID='+CF+'\n', encoding="utf-8")
    monkeypatch.delenv('CLOUDFRONT_DISTRIBUTION_ID',raising=False)
    assert distribution_id(str(env))==CF
    monkeypatch.setenv('CLOUDFRONT_DISTRIBUTION_ID','EABCDEF1234567')
    assert distribution_id(str(env))=='EABCDEF1234567'
    monkeypatch.setenv('CLOUDFRONT_DISTRIBUTION_ID','not valid')
    with pytest.raises(ValueError):distribution_id(str(env))


def test_one_aws_read_command_and_no_writes():
    payload={'MetricDataResults':[metric_result('m0',NOW-35,3)]}
    with patch('adaptivenet.ingestion.cloudfront.subprocess.run', return_value=Mock(returncode=0,stdout=json.dumps(payload))) as run:
        got=collect_cloudfront(CF,'my-sso',NOW)
    assert len(got)==1
    cmd=run.call_args.args[0]
    assert cmd[:3]==['aws','cloudwatch','get-metric-data']
    assert cmd[cmd.index('--region')+1]=='us-east-1'
    assert cmd[cmd.index('--profile')+1]=='my-sso'
    assert run.call_args.kwargs['check'] is False
    assert '--output' in cmd


def test_dashboard_exposes_cloudfront_and_all_regional_features():
    assert 'CloudFront delivery' in HTML
    assert 'Performance trends' in HTML
    assert 'Data quality gates' in HTML
    assert 'Feature explorer' in HTML
    assert "Object.entries(f)" in HTML
    assert "Object.entries(cfLabels)" in HTML
    assert '/v1/features/history?limit=120' in HTML
    assert 'Auto refresh: on' in HTML
    assert "No AWS actions" in HTML


def test_configure_cloudfront_preserves_other_env_settings(tmp_path):
    sys.path.insert(0,str(ROOT/'scripts'))
    from cloudfront_configure import save_distribution
    env=tmp_path/'.env'
    env.write_text('ATLAS_MEASUREMENTS=region-a=12,region-b=13\nLAB_TOKEN=safe\n', encoding="utf-8")
    backups=tmp_path/'backups'
    assert save_distribution(env,CF,backups) is True
    assert save_distribution(env,CF,backups) is False
    assert 'ATLAS_MEASUREMENTS=region-a=12,region-b=13' in env.read_text(encoding="utf-8")
    assert 'CLOUDFRONT_DISTRIBUTION_ID='+CF in env.read_text(encoding="utf-8")
    assert len(list(backups.iterdir()))==1
    with pytest.raises(ValueError):save_distribution(env,'bad;injected',backups)


def test_cycle_cloudfront_additive_no_aws_writes(tmp_path):
    from adaptivenet.ingestion.cli import cycle
    from adaptivenet.ingestion.sources import Collection
    src=[Observation('region-a','aws_amp',m,v,'unit',NOW,NOW) for m,v in [('app_up',1),('memory_available_bytes',600),('memory_total_bytes',1000)]]
    src += [Observation('region-b','aws_amp',m,v,'unit',NOW,NOW) for m,v in [('app_up',1),('memory_available_bytes',600),('memory_total_bytes',1000)]]
    cf=parse_metric_data({'MetricDataResults':[metric_result('m0',NOW-40,13)]},CF,NOW)
    with patch('adaptivenet.ingestion.cli.time.time',side_effect=[NOW,NOW+1]):
        with patch('adaptivenet.ingestion.cli.collect_all',return_value=Collection(src,'ok',[])):
            with patch('adaptivenet.ingestion.cli.region_configs',return_value=[]):
                with patch('adaptivenet.ingestion.cli.distribution_id',return_value=CF):
                    with patch('adaptivenet.ingestion.cli.collect_cloudfront',return_value=cf):
                        snap, stats=cycle(tmp_path/'one.db')
    assert stats['inserted']==7
    assert snap['ready_for_internal_only_model'] is True
    assert snap['cloudfront']['status']=='fresh'
    assert snap['cloudfront']['features']['cloudfront_requests']==13
