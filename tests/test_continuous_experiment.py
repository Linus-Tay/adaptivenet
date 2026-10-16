"""No-cloud regression tests for live experiment supervision, witnesses and guarded SSM."""
import json
from unittest.mock import patch
from urllib.error import HTTPError
import pytest
from adaptivenet.ingestion.witness import append_probe, recent_probes
from scripts import aws_fault, continuous_probe, live_monitor

def test_witness_journal_is_bounded_and_never_saves_extra_fields(tmp_path):
    p=tmp_path/'probe-events.jsonl'
    append_probe(p,{'timestamp':'2026-10-16T00:00:00Z','scope':'region-a',
                    'http_status':503,'outcome':'http_error','secret':'never-write'})
    append_probe(p,{'scope':'cloudfront','http_status':200,'outcome':'ok'})
    assert len(recent_probes(p))==2
    assert recent_probes(p)[0]['scope']=='cloudfront'
    assert recent_probes(p)[1]['http_status']==503
    assert 'secret' not in p.read_text()
    assert recent_probes(tmp_path/'missing')==[]

def test_monitor_has_no_implicit_http_traffic_or_aws_fault():
    commands=live_monitor.command_set(profile='demo',interval=120,traffic=False,probe_interval=60)
    assert len(commands)==2
    assert all('continuous_probe.py' not in ' '.join(cmd) for cmd,_ in commands)
    enabled=live_monitor.command_set(profile='demo',interval=120,traffic=True,probe_interval=90)
    assert len(enabled)==3
    assert '--run' in enabled[2][0]

def test_fault_restore_scheduled_before_stop():
    command=aws_fault.fault_script(30)
    assert command.index('systemd-run')<command.index('systemctl stop')
    assert '--on-active=30s' in command
    assert 'adaptivenet-demo.service' in command
    assert 'iptables' not in command
    for duration in (-1,0,9,61,10000):
        with pytest.raises(ValueError): aws_fault.fault_script(duration)

def test_ssm_target_checks_exact_account_tags_and_running():
    account={'Account':'468354413374'}
    instance={'Reservations':[{'Instances':[{'InstanceId':'i-0039feaedbd2b3266',
        'State':{'Name':'running'},'Tags':[{'Key':'Project','Value':'AdaptiveNet'},
                                          {'Key':'Environment','Value':'demo'}]}]}]}
    with patch.object(aws_fault,'aws_read',side_effect=[account,instance]):
        assert aws_fault.validate_target('p','468354413374','region-a')==(
            'ap-southeast-1','i-0039feaedbd2b3266')
    with patch.object(aws_fault,'aws_read',return_value=account):
        with pytest.raises(RuntimeError,match='account mismatch'):
            aws_fault.validate_target('p','000000000000','region-a')
    instance['Reservations'][0]['Instances'][0]['Tags']=[{'Key':'Project','Value':'other'}]
    with patch.object(aws_fault,'aws_read',side_effect=[account,instance]):
        with pytest.raises(RuntimeError,match='not the expected'):
            aws_fault.validate_target('p','468354413374','region-a')

def test_ssm_dry_run_does_not_write():
    with patch.object(aws_fault,'validate_target',return_value=('ap-southeast-1','i-0039feaedbd2b3266')):
        with patch.object(aws_fault,'aws_write') as write:
            assert aws_fault.main(['--region','region-a','--expected-account','468354413374'])==0
            write.assert_not_called()

def test_explicit_live_ssm_command_is_allowlisted_and_auditable(tmp_path):
    with patch.object(aws_fault,'validate_target',return_value=('ap-southeast-1','i-0039feaedbd2b3266')):
        with patch.object(aws_fault,'aws_write',return_value={'Command':{'CommandId':'test-id'}}) as write:
            assert aws_fault.main(['--region','region-a','--expected-account','468354413374',
                '--duration','20','--execute','--confirm','STOP-REGION-A',
                '--out',str(tmp_path/'journal.jsonl')])==0
    cmd=write.call_args.args[0]
    assert cmd[:2]==['ssm','send-command']
    assert 'AWS-RunShellScript' in cmd
    assert cmd[cmd.index('--instance-ids')+1]=='i-0039feaedbd2b3266'
    assert recent_probes(tmp_path/'journal.jsonl')[0]['outcome']=='ssm_submitted'

def test_http_503_is_recorded_as_failure_not_availability():
    with patch.object(continuous_probe,'urlopen',
                      side_effect=HTTPError('http://example',503,'Fault',{},None)):
        result=continuous_probe.probe('region-a','http://example/api/catalog')
    assert result['http_status']==503 and result['outcome']=='http_error'

def test_unsafe_probe_interval_rejected():
    with pytest.raises(SystemExit): continuous_probe.main(['--interval','30'])
