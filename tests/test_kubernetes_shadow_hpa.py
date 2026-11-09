from datetime import datetime, timezone
import json

import httpx
import pytest

from adaptivenet.controller.kubernetes_shadow import KubernetesShadowHpa
from adaptivenet.controller.scaling import CapacityRecommendation, WorkloadTarget
from adaptivenet.controller.shadow import assess_shadow
from adaptivenet.ingestion.store import Store
from scripts.model_handoff import main

NOW = 1_800_000_000
TARGET = WorkloadTarget("fixture-cluster", "adaptivenet", "catalog", "catalog-hpa")
HPA = "/apis/autoscaling/v2/namespaces/adaptivenet/horizontalpodautoscalers/catalog-hpa"
DEPLOYMENT = "/apis/apps/v1/namespaces/adaptivenet/deployments/catalog"
PODS = "/api/v1/namespaces/adaptivenet/pods"
METRICS = "/apis/metrics.k8s.io/v1beta1/namespaces/adaptivenet/pods"


def timestamp(seconds_ago):
    return datetime.fromtimestamp(NOW - seconds_ago, timezone.utc).isoformat()


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr("adaptivenet.controller.kubernetes_shadow.time.time", lambda: NOW)
    documents = {
        HPA: {
            "metadata": {"namespace": "adaptivenet", "name": "catalog-hpa"},
            "spec": {
                "scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": "catalog"},
                "minReplicas": 2, "maxReplicas": 6,
                "metrics": [{
                    "type": "Resource",
                    "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": 60}},
                }],
            },
            "status": {
                "currentReplicas": 2, "desiredReplicas": 2,
                "conditions": [
                    {"type": "AbleToScale", "status": "True"},
                    {"type": "ScalingActive", "status": "True"},
                    {"type": "ScalingLimited", "status": "False"},
                ],
                "currentMetrics": [{
                    "type": "Resource", "resource": {"name": "cpu", "current": {"averageUtilization": 81}},
                }],
            },
        },
        DEPLOYMENT: {
            "metadata": {"namespace": "adaptivenet", "name": "catalog", "generation": 2},
            "spec": {"selector": {"matchLabels": {"app": "catalog"}}},
            "status": {"observedGeneration": 2, "readyReplicas": 2, "availableReplicas": 2},
        },
        PODS: {"items": [
            {
                "metadata": {"namespace": "adaptivenet", "name": name, "labels": {"app": "catalog"}},
                "status": {
                    "phase": "Running",
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "containerStatuses": [{"restartCount": 0}],
                },
            }
            for name in ("catalog-a", "catalog-b")
        ]},
        METRICS: {"items": [
            {
                "metadata": {"namespace": "adaptivenet", "name": name},
                "timestamp": timestamp(age),
                "containers": [{"name": "demo-api", "usage": {"cpu": "83m", "memory": "10Mi"}}],
            }
            for name, age in (("catalog-a", 20), ("catalog-b", 10))
        ]},
    }
    requests = []

    def respond(request):
        assert request.method == "GET"
        path = request.url.path
        requests.append(path)
        if path in (PODS, METRICS):
            assert request.url.params["labelSelector"] == "app=catalog"
        if path not in documents:
            return httpx.Response(403)
        return httpx.Response(200, json=documents[path])

    with httpx.Client(base_url="https://fixture.invalid", transport=httpx.MockTransport(respond)) as client:
        yield KubernetesShadowHpa({"region-a": TARGET}, {TARGET.cluster: client}), documents, requests


def test_reads_only_allowlisted_resources_and_uses_oldest_pod_metric(api):
    reader, _, requests = api
    result = reader.assess("region-a")
    assert result.recommendation == CapacityRecommendation("recommend", "one_replica_within_hpa_bounds", 3)
    assert result.observation.observed_at == NOW
    assert result.observation.metrics_observed_at == NOW - 20
    assert (result.observation.cpu_current_pct, result.observation.cpu_target_pct) == (81, 60)
    assert requests == [HPA, DEPLOYMENT, PODS, METRICS]


def test_recent_api_poll_does_not_hide_a_stale_pod_metric(api):
    reader, documents, _ = api
    documents[METRICS]["items"][1]["timestamp"] = timestamp(61)
    result = reader.assess("region-a")
    assert result.observation.metrics_observed_at == NOW - 61
    assert result.recommendation.reason == "stale_or_invalid_hpa_evidence"


@pytest.mark.parametrize("missing_or_future", ["missing", "future"])
def test_metrics_require_fresh_timestamps_for_every_ready_pod(api, missing_or_future):
    reader, documents, _ = api
    if missing_or_future == "missing":
        documents[METRICS]["items"].pop()
    else:
        documents[METRICS]["items"][1]["timestamp"] = timestamp(-1)
    result = reader.assess("region-a")
    assert result.observation.metrics_observed_at is None
    assert result.recommendation.reason == "stale_or_invalid_hpa_evidence"


def test_shadow_reads_pending_pod_and_hpa_desired_without_duplicate_recommendation(api):
    reader, documents, _ = api
    documents[HPA]["status"]["desiredReplicas"] = 3
    documents[PODS]["items"].append({
        "metadata": {"namespace": "adaptivenet", "name": "catalog-c", "labels": {"app": "catalog"}},
        "status": {"phase": "Pending"},
    })
    result = reader.assess("region-a")
    assert result.observation.pending == 1
    assert result.recommendation == CapacityRecommendation("already_in_progress", "hpa_already_requested_capacity")


def test_workload_mismatch_stops_before_reading_pods(api):
    reader, documents, requests = api
    documents[HPA]["spec"]["scaleTargetRef"]["name"] = "other-workload"
    assert reader.assess("region-a").recommendation.reason == "invalid_or_mismatched_kubernetes_state"
    assert requests == [HPA]


def test_mismatched_hpa_generation_is_blocked(api):
    reader, documents, _ = api
    documents[HPA]["metadata"]["generation"] = 2
    documents[HPA]["status"]["observedGeneration"] = 1
    assert reader.assess("region-a").recommendation.reason == "invalid_or_mismatched_kubernetes_state"


def test_missing_context_or_permission_fails_closed(api):
    reader, documents, requests = api
    assert reader.assess("region-b").recommendation.reason == "unknown_affected_region"
    assert requests == []
    missing_context = KubernetesShadowHpa({"region-a": TARGET}, {})
    assert missing_context.assess("region-a").recommendation.reason == "missing_kubernetes_context"
    documents.pop(METRICS)
    assert reader.assess("region-a").recommendation.reason == "kubernetes_read_http_403"


def feature_snapshot():
    return {
        "contract_version": "adaptivenet.features.v1",
        "snapshot_id": "features-fixture", "computed_at_unix": NOW,
        "ready_for_model": True, "ready_for_internal_only_model": True,
        "regions": {
            region: {
                "features": {"app_up": 1, "cpu_busy_pct": 25, "memory_available_pct": 60},
                "quality": {"internal": "fresh"},
            }
            for region in ("region-a", "region-b")
        },
    }


def scale_proposal(model_input):
    assert model_input["contract_version"] == "adaptivenet.model_input.v1"
    return {
        "contract_version": "adaptivenet.proposal.v1", "snapshot_id": model_input["snapshot_id"],
        "action": "scale_out", "confidence": 0.95, "affected_region": "region-a",
        "model_id": "fixture", "evidence": ["fixture_cpu"],
    }


def test_scale_shadow_pipeline_checks_proposal_and_reads_hpa_without_execution(api):
    reader, _, requests = api
    result = assess_shadow(feature_snapshot(), scale_proposal, hpa=reader)
    assert result["state"] == "capacity_review_only"
    assert result["capacity"]["recommendation"]["requested_replicas"] == 3
    assert result["executed"] is False
    assert result["effective_action"] == "hold"
    assert result["decision_review"]["approved_for_execution"] is False
    assert requests == [HPA, DEPLOYMENT, PODS, METRICS]


def test_invalid_proposal_and_stale_inputs_do_not_contact_kubernetes(api):
    reader, _, requests = api
    stale = {**feature_snapshot(), "computed_at_unix": NOW - 181}

    def unexpected_predictor(_model_input):
        pytest.fail("stale telemetry must not reach the predictor")

    assert assess_shadow(stale, unexpected_predictor, hpa=reader)["state"] == "blocked"

    def wrong_snapshot(model_input):
        return {**scale_proposal(model_input), "snapshot_id": "different"}

    assert assess_shadow(feature_snapshot(), wrong_snapshot, hpa=reader)["reasons"] == ["snapshot_id_mismatch"]
    assert requests == []


def test_shift_shadow_review_requires_explicit_current_route(api):
    reader, _, requests = api

    def shift(model_input):
        return {
            **scale_proposal(model_input), "action": "shift_traffic", "target_region_a_percent": 75,
        }

    assert assess_shadow(feature_snapshot(), shift, hpa=reader)["reasons"] == ["invalid_current_route_weight"]
    result = assess_shadow(feature_snapshot(), shift, hpa=reader, current_region_a_percent=100)
    assert result["state"] == "route_review_only"
    assert result["executed"] is False
    assert requests == []


def test_cli_reads_feature_db_and_runs_team_predictor_without_hpa(api, tmp_path, capsys):
    db = tmp_path / "features.db"
    Store(db).store_snapshot(feature_snapshot())
    assert main(["shadow", "--db", str(db)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["proposal"]["model_id"] == "conservative_rule_baseline_v1"
    assert result["proposal"]["action"] == "hold"
    assert result["executed"] is False


def test_cli_reports_missing_cluster_without_actuating(api, tmp_path, capsys):
    snapshot_path = tmp_path / "snapshot.json"
    proposal_path = tmp_path / "proposal.json"
    snapshot = feature_snapshot()
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    proposal_path.write_text(json.dumps(scale_proposal(snapshot | {
        "contract_version": "adaptivenet.model_input.v1",
    })), encoding="utf-8")
    assert main(["shadow", "--snapshot", str(snapshot_path), "--proposal", str(proposal_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "blocked"
    assert result["reasons"] == ["missing_kubernetes_context"]
    assert result["executed"] is False
