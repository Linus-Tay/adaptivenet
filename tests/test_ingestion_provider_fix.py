"""Live-response regression tests; all providers mocked (no network, AWS or credits)."""
from __future__ import annotations
import json
import sys
import types
from pathlib import Path
from unittest.mock import patch
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services'))
sys.path.insert(0,str(ROOT/'scripts'))
from adaptivenet.ingestion.config import RegionConfig
from adaptivenet.ingestion.sources import query_amp, parse_atlas_latest, parse_prometheus, UpstreamDataError
from ingestion_doctor import amp as doctor_amp, atlas as doctor_atlas

NOW=1791965200.0
CFG=RegionConfig('region-a','ap-southeast-1','ws-12cfc829-fd81-440a-8c13-38cd28123fef',222734302)

def response(s):
    return types.SimpleNamespace(stdout=json.dumps(s),stderr='',returncode=0)

def good():
    return {'status':'success','data':{'resultType':'vector','result':[{'value':[NOW-5,'1']} ]}}

def test_prometheus_uses_documented_form_encoded_post_and_sso_profile():
    def fake(cmd, **kw):
        assert cmd[:5]==['awscurl','--region','ap-southeast-1','--service','aps']
        assert cmd[cmd.index('-X')+1]=='POST'
        assert cmd[cmd.index('--header')+1]=='Content-Type: application/x-www-form-urlencoded'
        assert cmd[-2]=='-d'
        assert 'query=max%28adaptivenet_demo_up%7B' in cmd[-1]
        assert 'region%3D%22region-a%22' in cmd[-1]
        assert '/api/v1/query' in cmd[cmd.index('POST')+1]
        assert '?' not in cmd[cmd.index('POST')+1]
        assert kw['env']['AWS_PROFILE']=='adaptivenet-admin'
        return response(good())
    with patch('adaptivenet.ingestion.sources.subprocess.run',side_effect=fake):
        o=query_amp(CFG,'app_up','adaptivenet-admin',NOW)
    assert o.value==1 and o.source=='aws_amp'

def test_message_only_aws_responses_explain_errors_without_leaks():
    cases={
        'User arn:aws:sts::123:assumed-role/whatever is not authorized to perform: aps:QueryMetrics':'amp_access_denied',
        'Missing Authentication Token':'amp_endpoint_or_method_error',
        'Request signature we calculated does not match':'amp_authentication_failed',
        'The query is invalid; bad_data':'amp_query_rejected',
        'Too Many Requests: throttling':'amp_rate_limited',
    }
    for msg,expected in cases.items():
        with pytest.raises(UpstreamDataError,match=expected):
            parse_prometheus({'message':msg},NOW)
        with patch('ingestion_doctor.subprocess.run',return_value=response({'message':msg})):
            diag=doctor_amp(CFG,'adaptivenet-admin','app_up',NOW)
        assert diag['diagnostic_code']==expected
        assert msg not in json.dumps(diag)


def ping(probe,ts,sent=3,rtts=(10,20),msm_id=222734302):
    return {'prb_id':probe,'msm_id':msm_id,'timestamp':ts,'sent':sent,
            'result':[{'rtt':r} for r in rtts] + [{}]*(sent-len(rtts))}

def test_ripe_flat_list_is_accepted_and_duplicate_probe_prefers_latest():
    flat=[ping(10,NOW-100,rtts=(40,)),ping(10,NOW-30,rtts=(21,31)),
          ping(11,NOW-20,rtts=()),ping(12,NOW-10,msm_id=9999)]
    events=parse_atlas_latest(flat,CFG,NOW)
    assert len(events)==3
    p10=[e for e in events if e.source_id.endswith('/10')]
    assert len(p10)==2 and all(e.observed_at==NOW-30 for e in p10)
    assert next(e.value for e in p10 if e.metric=='network_rtt_ms')==26
    p11=[e for e in events if e.source_id.endswith('/11')]
    assert len(p11)==1 and p11[0].metric=='packet_loss_pct' and p11[0].value==100
    assert all(e.source_id!='222734302/12' for e in events)


def test_ripe_probe_keyed_documented_payload_still_works():
    raw={'22':[ping(22,NOW-6)],'23':[ping(23,NOW-15,rtts=())]}
    events=parse_atlas_latest(raw,CFG,NOW)
    assert len(events)==3
    assert len({e.source_id for e in events})==2
    assert parse_atlas_latest([],CFG,NOW)==[]
    with pytest.raises(UpstreamDataError,match='atlas_response_not_object_or_list'):
        parse_atlas_latest('unexpected',CFG,NOW)


def test_doctor_can_parse_flat_atlas_list_with_real_timestamps():
    class Stream:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def read(self,*args):return json.dumps([ping(22,NOW-5)]).encode()
    with patch('ingestion_doctor.urlopen',return_value=Stream()):
        diag=doctor_atlas(CFG,NOW)
    assert diag['status']=='ok' and diag['shape']=='list'
    assert diag['distinct_probes']==1 and diag['parsed_observations']==2
    assert diag['newest_age_seconds']==5


def test_atlas_old_results_remain_timestamped_historical_not_freshened():
    old=NOW-3600
    events=parse_atlas_latest([ping(23,old)],CFG,NOW)
    assert events and all(e.observed_at==old for e in events)
