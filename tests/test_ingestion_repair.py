"""Offline regression tests for upstream errors and query contracts."""
import sys
import json
from pathlib import Path
from unittest.mock import patch
import types
import pytest
P=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(P/'services'))
sys.path.insert(0,str(P/'scripts'))
from adaptivenet.ingestion.config import RegionConfig
from adaptivenet.ingestion.sources import (
    parse_prometheus, UpstreamDataError, query_amp, collect_all, PROMQL,
)
from adaptivenet.ingestion.models import Observation
from ingestion_doctor import amp

NOW=1791965200.0
CFG=RegionConfig('region-a','ap-southeast-1','ws-test',222734302)

def ok_response(value='1'):
    return json.dumps({'status':'success','data':{'resultType':'vector','result':[{'metric':{},'value':[NOW-12,value]}]}})

def test_parse_prom_valid_value():
    r=parse_prometheus(json.loads(ok_response()),NOW)
    assert r==(NOW-12,1.0)

def test_amp_rejects_status_error_with_safe_type():
    with pytest.raises(UpstreamDataError,match='amp_api_error_bad_data'):
        parse_prometheus({'status':'error','errorType':'bad_data','error':'a user-supplied secret'},NOW)

def test_amp_rejects_wrong_result_shape_with_code():
    with pytest.raises(UpstreamDataError,match='amp_result_type_scalar'):
        parse_prometheus({'status':'success','data':{'resultType':'scalar','result':[NOW,1]}},NOW)

def test_amp_smoke_uses_expected_promql_and_parses_realistic_aws_json():
    def call(cmd,**kw):
        assert '--region' in cmd and '--service' in cmd
        assert 'query=' in cmd[-1]
        from urllib.parse import urlparse,parse_qs
        assert '-X' in cmd and cmd[cmd.index('-X')+1]=='POST'
        assert '/api/v1/query' in cmd[cmd.index('POST')+1]
        q=parse_qs(cmd[-1])['query'][0]
        assert q==PROMQL['app_up'][0].replace('{name}','region-a')
        return types.SimpleNamespace(returncode=0,stdout=ok_response(),stderr='')
    with patch('adaptivenet.ingestion.sources.subprocess.run',side_effect=call):
        x=query_amp(CFG,'app_up','adaptivenet-admin',NOW)
    assert isinstance(x,Observation) and x.value==1

def test_doctor_summarizes_response_without_raw_error_text():
    bad=json.dumps({'status':'error','errorType':'bad_data','error':'EXAMPLE_SECRET_TOKEN'})
    with patch('ingestion_doctor.subprocess.run',return_value=types.SimpleNamespace(returncode=0,stdout=bad,stderr='')):
        r=amp(CFG,'adaptivenet-admin','app_up',NOW)
    assert r['status']=='error' and r['error_type']=='bad_data'
    assert 'EXAMPLE_SECRET_TOKEN' not in json.dumps(r)

def test_collector_reports_specific_safe_codes_without_crashing():
    with patch('adaptivenet.ingestion.sources.query_amp',side_effect=UpstreamDataError('amp_api_error_bad_data')):
        with patch('adaptivenet.ingestion.sources.query_atlas',side_effect=UpstreamDataError('atlas_response_not_object')):
            data=collect_all([CFG],'adaptivenet-admin',NOW,max_workers=2)
    assert len(data.observations)==0
    assert 'region-a:app_up:amp_api_error_bad_data' in data.errors
    assert 'region-a:atlas:atlas_response_not_object' in data.errors
