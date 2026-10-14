"""No-network regression coverage for the timestamp skew and live metric doctor."""
from pathlib import Path
import json
import sys
from unittest.mock import patch, Mock
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services'))
sys.path.insert(0,str(ROOT/'scripts'))
from adaptivenet.ingestion.models import Observation
from adaptivenet.ingestion.features import AMP_CLOCK_SKEW_SEC,make_snapshot
from adaptivenet.ingestion.cli import cycle
from adaptivenet.ingestion.sources import Collection
import amp_metric_doctor

BASE=1800000000.0

def item(r,m,v,t,source='aws_amp',collected=BASE):
    return Observation(r,source,m,v,'unit',t,collected,'test-amp' if source=='aws_amp' else '42/1')

def test_aws_clock_ahead_bounded_and_provenance():
    obs=[]
    for r in ('region-a','region-b'):
        for metric,value in [('app_up',1),('memory_total_bytes',1000),('memory_available_bytes',550),('cpu_busy_pct',5)]:
            obs.append(item(r,metric,value,BASE+9))
    snap=make_snapshot(obs,BASE+2)
    assert snap['ready_for_internal_only_model'] is True
    assert snap['ready_for_model'] is False
    assert snap['regions']['region-b']['features']['memory_available_pct']==55
    info=snap['regions']['region-b']['provenance']['app_up']
    assert info['clock_skew_ahead_seconds']==7
    assert info['age_seconds']==0
    assert info['observed_at'].startswith('2027')

def test_future_sample_outside_skew_and_uncollected_sample_rejected():
    valid=[item('region-b',m,v,BASE+31) for m,v in [('app_up',1),('memory_total_bytes',1000),('memory_available_bytes',600)]]
    s=make_snapshot(valid,BASE)
    assert s['regions']['region-b']['quality']['internal']=='missing'
    not_yet_collected=[item('region-b',m,v,BASE+4,collected=BASE+10) for m,v in [('app_up',1),('memory_total_bytes',1000),('memory_available_bytes',600)]]
    s2=make_snapshot(not_yet_collected,BASE)
    assert s2['regions']['region-b']['quality']['internal']=='missing'

def test_ripe_future_or_stale_not_freshened_by_aws_allowance():
    points=[]
    for idx in range(5):
        points.extend([item('region-a','packet_loss_pct',0,BASE+4,'ripe_atlas'),item('region-a','network_rtt_ms',15,BASE+4,'ripe_atlas')])
    # Existing probe dedupe also applies; future RIPE observations NEVER qualify.
    assert make_snapshot(points,BASE)['regions']['region-a']['quality']['external']=='insufficient_or_stale'

def test_cycle_reads_bounded_future_aws_samples_from_sqlite(tmp_path):
    obs=[]
    for r in ('region-a','region-b'):
        for m,v in [('app_up',1),('memory_total_bytes',1000),('memory_available_bytes',600)]:
            obs.append(item(r,m,v,BASE+7))
    with patch('adaptivenet.ingestion.cli.time.time',side_effect=[BASE,BASE+1]):
        with patch('adaptivenet.ingestion.cli.region_configs',return_value=[]):
            with patch('adaptivenet.ingestion.cli.collect_all',return_value=Collection(obs,'ok',[])):
                snap,stats=cycle(tmp_path/'journal.db')
    assert stats['inserted']==6
    assert snap['ready_for_internal_only_model'] is True
    assert snap['regions']['region-a']['quality']['internal']=='fresh'
    assert snap['regions']['region-b']['quality']['internal']=='fresh'


def test_safe_post_promql_diagnostic_and_no_secrets():
    cfg=Mock(name='region-a',aws_region='ap-southeast-1',workspace='ws-abc')
    payload={'status':'success','data':{'resultType':'vector','result':[{'metric':{'route':'/api/catalog','job':'adaptivenet_demo','instance':'private-instance','project':'adaptivenet'},'value':[BASE,'5']} ]}}
    with patch('amp_metric_doctor.subprocess.run',return_value=Mock(returncode=0,stdout=json.dumps(payload))) as proc:
        result=amp_metric_doctor.query(cfg,'adaptivenet_http_requests_total', 'adaptivenet-admin')
    assert result['state']=='present'
    assert result['first_value']==5
    assert 'instance' not in result['first_series_labels']
    args=proc.call_args.args[0]
    assert args[args.index('-X')+1]=='POST'
    assert 'query=adaptivenet_http_requests_total' in args[-1]
    assert proc.call_args.kwargs['env']['AWS_PROFILE']=='adaptivenet-admin'

def test_clock_skew_constant_is_bounded():
    assert 0 < AMP_CLOCK_SKEW_SEC <= 30
