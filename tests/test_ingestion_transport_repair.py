"""Mac compatibility and read-only transport tests: no network or AWS calls."""
from pathlib import Path
from unittest.mock import patch
import json
import os
import sys
import types
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'services'))
sys.path.insert(0, str(ROOT/'scripts'))
from adaptivenet.ingestion.sources import (query_amp, parse_prometheus, UpstreamDataError,
                                           atlas_tls_context, query_atlas)
from adaptivenet.ingestion.config import RegionConfig
from ingestion_doctor import amp

NOW=1791965200.0
CFG=RegionConfig('region-a','ap-southeast-1','ws-test',222734302)

def good_json():
    return json.dumps({'status':'success','data':{'resultType':'vector','result':[{'metric':{}, 'value':[NOW-8,'1']}]}})

def test_aws_profile_matches_known_working_mac_command():
    def fake_run(cmd, **kwargs):
        assert cmd[:5]==['awscurl','--region','ap-southeast-1','--service','aps']
        assert '--profile' not in cmd
        assert kwargs['env']['AWS_PROFILE']=='adaptivenet-admin'
        assert 'query=' in cmd[-1]
        return types.SimpleNamespace(returncode=0,stdout=good_json(),stderr='')
    with patch('adaptivenet.ingestion.sources.subprocess.run',side_effect=fake_run):
        obs=query_amp(CFG,'app_up','adaptivenet-admin',NOW)
    assert obs.value==1 and obs.region=='region-a'

def test_aws_error_shape_is_safely_classified():
    with pytest.raises(UpstreamDataError,match='amp_api_error_AccessDeniedException'):
        parse_prometheus({'__type':'AccessDeniedException','message':'Secret-string-here'},NOW)
    with pytest.raises(UpstreamDataError,match='amp_api_error_ThrottlingException'):
        parse_prometheus({'Code':'ThrottlingException','Message':'secret'},NOW)

def test_aws_non_json_is_safe_error():
    with patch('adaptivenet.ingestion.sources.subprocess.run',return_value=types.SimpleNamespace(returncode=0,stdout='<html>bad</html>',stderr='')):
        with pytest.raises(UpstreamDataError,match='amp_non_json_response'):
            query_amp(CFG,'app_up','adaptivenet-admin',NOW)

def test_atlas_explicit_ca_bundle_is_passed_to_verified_ssl(tmp_path):
    p=tmp_path/'ca.pem'; p.write_text('dummy')
    with patch.dict(os.environ,{'SSL_CERT_FILE':str(p)}), patch('adaptivenet.ingestion.sources.ssl.create_default_context',return_value='context') as mocked:
        assert atlas_tls_context()=='context'
        mocked.assert_called_once_with(cafile=str(p))

def test_atlas_missing_explicit_ca_fails_closed(tmp_path):
    with patch.dict(os.environ,{'SSL_CERT_FILE':str(tmp_path/'missing') }):
        with pytest.raises(UpstreamDataError,match='atlas_ca_file_missing'):
            atlas_tls_context()

def test_doctor_safe_aws_shape_no_message_leak():
    with patch('ingestion_doctor.subprocess.run',return_value=types.SimpleNamespace(returncode=0,stdout=json.dumps({'__type':'AccessDeniedException','message':'PRIVATE-DATA'}),stderr='')):
        r=amp(CFG,'adaptivenet-admin','app_up',NOW)
    assert r['status']=='absent' and r['error_type']=='AccessDeniedException'
    assert r['top_level_keys']==['__type','message']
    assert 'PRIVATE-DATA' not in json.dumps(r)
