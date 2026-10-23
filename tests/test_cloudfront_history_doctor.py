"""CloudFront diagnostics show real historic samples without treating them as live."""
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, Mock
import json
from scripts.cloudfront_doctor import history_diagnostics


def test_cloudwatch_diagnosis_distinguishes_fresh_stale_and_absent():
    now=datetime(2026,10,16,5,0,tzinfo=timezone.utc)
    def iso(minutes): return (now-timedelta(minutes=minutes)).isoformat()
    payload={"MetricDataResults":[
        {"Id":"m0","StatusCode":"Complete","Timestamps":[iso(2),iso(60)],"Values":[5,50]},
        {"Id":"m1","StatusCode":"Complete","Timestamps":[iso(40)],"Values":[0.0]},
        {"Id":"m2","StatusCode":"Complete","Timestamps":[],"Values":[]}
    ]}
    with patch('scripts.cloudfront_doctor.subprocess.run',
               return_value=Mock(returncode=0,stdout=json.dumps(payload))) as run:
        result=history_diagnostics('E1234567890ABC','demo',now=now)
    items=result['metric_samples']
    assert items['cloudfront_requests']['sample_count']==2
    assert items['cloudfront_requests']['latest_value']==5
    assert items['cloudfront_requests']['within_live_freshness'] is True
    assert items['cloudfront_4xx_pct']['latest_value']==0
    assert items['cloudfront_4xx_pct']['within_live_freshness'] is False
    assert items['cloudfront_5xx_pct']['sample_count']==0
    assert items['cloudfront_bytes_uploaded']['sample_count']==0
    args=run.call_args.args[0]
    assert args[:3]==['aws','cloudwatch','get-metric-data']
    assert args[args.index('--region')+1]=='us-east-1'
