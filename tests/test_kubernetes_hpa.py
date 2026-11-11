from datetime import datetime, timezone
import json

import httpx
import pytest

from adaptivenet.controller.kubernetes_hpa import KubernetesHpaReader
from adaptivenet.controller.scaling import CapacityRecommendation, WorkloadTarget

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
    monkeypatch.setattr("adaptivenet.controller.kubernetes_hpa.time.time", lambda: NOW)
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
        yield KubernetesHpaReader({"region-a": TARGET}, {TARGET.cluster: client}), documents, requests


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


def test_reads_pending_pod_and_hpa_desired_without_duplicate_recommendation(api):
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
    missing_context = KubernetesHpaReader({"region-a": TARGET}, {})
    assert missing_context.assess("region-a").recommendation.reason == "missing_kubernetes_context"
    documents.pop(METRICS)
    assert reader.assess("region-a").recommendation.reason == "kubernetes_read_http_403"
