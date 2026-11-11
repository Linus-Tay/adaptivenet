import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parents[1] / 'historical_amp_evidence.py'
spec = importlib.util.spec_from_file_location('historical_amp_evidence', HERE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def test_utc_requires_explicit_utc():
    assert m.parse_utc('2026-10-23T07:36:53Z').isoformat() == '2026-10-23T07:36:53+00:00'
    for invalid in ['2026-10-23T07:36:53', '2026-10-23T15:36:53+08:00']:
        try:
            m.parse_utc(invalid)
            assert False, 'Expected ValueError'
        except ValueError:
            pass


def test_original_sample_stamps_and_missing_nonfinite():
    p = {"status": "success", "data": {"resultType": "matrix", "result": [{
        "metric": {"region": "region-a", "job": "app", "secret": "must-not-include"},
        "values": [[10, "1"], [20, "0"], [30, "NaN"], [50, "0"]]
    }]}}
    r = m.extract_raw_samples(p, start_ts=15, end_ts=40)
    assert len(r) == 1
    assert r[0]['labels'] == {"region": "region-a", "job": "app"}
    assert [x['value'] for x in r[0]['samples']] == [0.0, None]
    assert [x['observed_at_unix'] for x in r[0]['samples']] == [20.0, 30.0]


def test_no_series_not_interpreted_as_zero():
    p = {"status": "success", "data": {"resultType": "matrix", "result": []}}
    assert m.extract_raw_samples(p, start_ts=0, end_ts=99) == []


def test_invalid_response_is_rejected():
    try:
        m.extract_raw_samples({"status":"error"}, start_ts=0, end_ts=20)
        assert False, 'Expected ValueError'
    except ValueError:
        pass


def test_historical_promql_preserves_prometheus_selector_braces():
    for region in ("region-a", "region-b"):
        for name in m.METRICS:
            expression = m.historical_expression(name, region, 480)
            assert expression.endswith("[480s]")
            assert expression.count("{") == 1
            assert expression.count("}") == 1
            assert "{region}" not in expression
            assert "project=\"adaptivenet\"" in expression
            if name != "scrape_up":
                assert f'region="{region}"' in expression
    assert m.historical_expression("app_up", "region-a", 480) == (
        'adaptivenet_demo_up{region="region-a",project="adaptivenet"}[480s]'
    )


def test_historical_cli_writes_actual_timestamped_samples_without_aws_writes(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import datetime
    monkeypatch.setattr(m, "region_configs", lambda _: [SimpleNamespace(
        name="region-a", aws_region="ap-southeast-1", workspace="ws-test")])
    queries = []
    def fake_fetch_matrix(**kwargs):
        queries.append(kwargs["expression"])
        sample_time = kwargs["end_unix"] - 30
        return {"status": "success", "data": {"resultType": "matrix", "result": [{
            "metric": {"region": "region-a", "project": "adaptivenet"},
            "values": [[sample_time, "1"]]
        }]}}
    monkeypatch.setattr(m, "fetch_matrix", fake_fetch_matrix)
    output = tmp_path / "history.json"
    rc = m.main(["--region", "region-a", "--start", "2026-10-23T07:34:00Z",
                 "--end", "2026-10-23T07:42:00Z", "--out", str(output)])
    assert rc == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["evidence_status"] == "UNREVIEWED_NOT_GROUND_TRUTH"
    assert len(queries) == 3
    assert all(q.endswith("[480s]") for q in queries)
    assert report["results"]["app_up"]["series"][0]["samples"][0]["value"] == 1
    assert report["results"]["app_up"]["series"][0]["samples"][0]["observed_at_utc"] == (
        "2026-10-23T07:41:30+00:00")
