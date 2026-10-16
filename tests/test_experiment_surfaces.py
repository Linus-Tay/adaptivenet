"""Guard against accidentally connecting the simulator or fault controls to observer."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def read(path):
    return (ROOT / path).read_text(encoding="utf-8")

def test_observer_has_no_mutation_route_and_uses_separate_probe_journal():
    api = read("services/adaptivenet/ingestion/api.py")
    assert "def do_GET" in api and "def do_POST" not in api
    assert '"/v1/experiments/probes"' in api
    assert 'recent_probes(' in api

def test_real_aws_mode_blocks_operator_browser_mutation():
    operator = read("apps/dashboard/app/api/lab/route.ts")
    assert "process.env.ADAPTIVENET_MODE==='aws'" in operator
    assert "status:403" in operator
    page = read("apps/dashboard/app/page.tsx")
    assert "aws_fault.py" in page and "LOCAL SIMULATOR" in page

def test_public_website_queries_real_cloudfront_and_both_origins_not_operator():
    route = read("apps/demo-web/app/api/catalog/route.ts")
    assert "DEMO_TARGET_URL" in route and "DEMO_REGION_A_URL" in route and "DEMO_REGION_B_URL" in route
    assert "POST" not in route
    page = read("apps/demo-web/app/page.tsx")
    assert "rows.cloudfront?.response?.items" in page
    assert '"/api/catalog?source="' in page

def test_live_monitor_traffic_is_explicit_and_no_faults_scheduled():
    monitor = read("scripts/live_monitor.py")
    assert "action='store_true'" in monitor or 'action="store_true"' in monitor
    assert "continuous_probe.py" in monitor
    assert "aws_fault.py" not in monitor
    assert "DEMO_TARGET_URL" in monitor
