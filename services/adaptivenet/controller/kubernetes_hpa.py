"""Read allowlisted Kubernetes workload state for capacity safety checks."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import partial
import time
from typing import TYPE_CHECKING, Mapping, Protocol
from urllib.parse import quote

if TYPE_CHECKING:
    import httpx

from .scaling import CapacityRecommendation, HpaObservation, WorkloadTarget, recommend_capacity


@dataclass(frozen=True)
class HpaAssessment:
    recommendation: CapacityRecommendation
    observation: HpaObservation | None = None


class KubernetesReadError(Exception):
    """An HTTP or transport failure while reading the Kubernetes API."""


class KubernetesReader(Protocol):
    def __call__(self, path: str, *, selector: str | None = None) -> dict: ...


class KubernetesHpaReader:
    """Use a caller-supplied, authenticated client for each configured cluster."""

    def __init__(
        self, allowlist: Mapping[str, WorkloadTarget],
        clients: Mapping[str, httpx.Client] | None = None, *,
        readers: Mapping[str, KubernetesReader] | None = None,
    ):
        self._allowlist = dict(allowlist)
        self._readers = {cluster: partial(_get, client) for cluster, client in (clients or {}).items()}
        self._readers.update(readers or {})

    def assess(self, affected_region: str | None) -> HpaAssessment:
        target = self._allowlist.get(affected_region) if isinstance(affected_region, str) else None
        if target is None:
            return HpaAssessment(CapacityRecommendation("blocked", "unknown_affected_region"))
        read = self._readers.get(target.cluster)
        if read is None:
            return HpaAssessment(CapacityRecommendation("blocked", "missing_kubernetes_context"))
        try:
            observation = _read_hpa(read, target)
        except KubernetesReadError as exc:
            reason = str(exc)
        except (KeyError, TypeError, ValueError, AttributeError):
            reason = "invalid_or_mismatched_kubernetes_state"
        else:
            return HpaAssessment(
                recommend_capacity(
                    affected_region, allowlist=self._allowlist, observation=observation, now=time.time(),
                ),
                observation,
            )
        return HpaAssessment(CapacityRecommendation("blocked", reason))


def _get(client: httpx.Client, path: str, *, selector: str | None = None) -> dict:
    import httpx

    params = {"labelSelector": selector} if selector is not None else None
    try:
        response = client.get(path, params=params, follow_redirects=False)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise KubernetesReadError(f"kubernetes_read_http_{exc.response.status_code}") from exc
    except httpx.RequestError as exc:
        raise KubernetesReadError("kubernetes_read_failed") from exc
    return response.json()


def _check_identity(resource: dict, *, namespace: str, name: str, require_generation: bool = True) -> None:
    metadata = resource["metadata"]
    if metadata["namespace"] != namespace or metadata["name"] != name:
        raise ValueError("resource identity mismatch")
    generation = metadata.get("generation")
    observed = resource["status"].get("observedGeneration")
    # Older HPA versions omit both generation fields.
    if not require_generation and generation in (None, 0) and observed is None:
        return
    if type(generation) is not int or generation <= 0 or observed != generation:
        raise ValueError("status has not observed the current spec")


def _items(document: dict) -> list[dict]:
    if document.get("metadata", {}).get("continue"):
        raise ValueError("incomplete list")
    items = document["items"]
    if not isinstance(items, list):
        raise ValueError("invalid list")
    return items


def _condition(status: dict, name: str) -> bool | None:
    values = [item["status"] for item in status.get("conditions", []) if item["type"] == name]
    if values == ["True"]:
        return True
    if values == ["False"]:
        return False
    return None


def _cpu_target(spec: dict) -> int:
    metric, = spec["metrics"]
    resource = metric["resource"]
    target = resource["target"]
    utilization = target["averageUtilization"]
    if (
        metric["type"] != "Resource" or resource["name"] != "cpu" or target["type"] != "Utilization"
        or type(utilization) is not int or utilization <= 0
    ):
        raise ValueError("unsupported HPA metric")
    return utilization


def _cpu_current(status: dict) -> int | None:
    for metric in status.get("currentMetrics", []):
        if metric["type"] == "Resource" and metric["resource"]["name"] == "cpu":
            value = metric["resource"]["current"].get("averageUtilization")
            return value if type(value) is int and value >= 0 else None
    return None


def _oldest_metrics_timestamp(metrics: list[dict], pods: set[str], namespace: str) -> float | None:
    by_pod = {}
    now = time.time()
    for metric in metrics:
        name = metric["metadata"]["name"]
        if name not in pods:
            continue
        if name in by_pod or metric["metadata"]["namespace"] != namespace:
            raise ValueError("ambiguous pod metrics")
        timestamp = metric.get("timestamp")
        containers = metric.get("containers")
        if not isinstance(timestamp, str) or not containers or not all(
            "cpu" in container.get("usage", {}) for container in containers
        ):
            return None
        parsed = datetime.fromisoformat(timestamp)
        if parsed.utcoffset() is None:
            return None
        measured_at = parsed.timestamp()
        if measured_at > now:
            return None
        by_pod[name] = measured_at
    if not pods or set(by_pod) != pods:
        return None
    return min(by_pod.values())


def _read_hpa(read: KubernetesReader, target: WorkloadTarget) -> HpaObservation:
    namespace, hpa_name, deployment_name = (
        quote(value, safe="") for value in (target.namespace, target.hpa, target.deployment)
    )
    hpa = read(f"/apis/autoscaling/v2/namespaces/{namespace}/horizontalpodautoscalers/{hpa_name}")
    observed_at = time.time()
    _check_identity(hpa, namespace=target.namespace, name=target.hpa, require_generation=False)
    spec = hpa["spec"]
    ref = spec["scaleTargetRef"]
    if (ref.get("apiVersion"), ref.get("kind"), ref.get("name")) != ("apps/v1", "Deployment", target.deployment):
        raise ValueError("HPA points at a different workload")
    status = hpa["status"]
    cpu_target = _cpu_target(spec)
    cpu_current = _cpu_current(status)

    deployment = read(f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment_name}")
    _check_identity(deployment, namespace=target.namespace, name=target.deployment)
    selector = deployment["spec"]["selector"]
    labels = selector["matchLabels"]
    if selector.get("matchExpressions") or not labels:
        raise ValueError("unsupported deployment selector")
    label_selector = ",".join(f"{key}={value}" for key, value in sorted(labels.items()))
    pods = _items(read(f"/api/v1/namespaces/{namespace}/pods", selector=label_selector))
    running = set()
    pending = 0
    restart_count = 0
    for pod in pods:
        metadata = pod["metadata"]
        if metadata["namespace"] != target.namespace or not all(
            metadata["labels"].get(key) == value for key, value in labels.items()
        ):
            raise ValueError("pod does not match deployment selector")
        if metadata.get("deletionTimestamp"):
            continue
        phase = pod["status"]["phase"]
        if phase == "Pending":
            pending += 1
        elif phase == "Running" and _condition(pod["status"], "Ready") is True:
            running.add(metadata["name"])
        restart_count += sum(item["restartCount"] for item in pod["status"].get("containerStatuses", []))

    deployment_status = deployment["status"]
    ready = deployment_status.get("readyReplicas", 0)
    metrics_observed_at = None
    if cpu_current is not None and len(running) == ready and running:
        metrics = _items(read(
            f"/apis/metrics.k8s.io/v1beta1/namespaces/{namespace}/pods", selector=label_selector,
        ))
        metrics_observed_at = _oldest_metrics_timestamp(metrics, running, target.namespace)
    return HpaObservation(
        target=target, observed_at=observed_at, metrics_observed_at=metrics_observed_at,
        min_replicas=spec["minReplicas"], max_replicas=spec["maxReplicas"],
        current=status["currentReplicas"], desired=status["desiredReplicas"], ready=ready,
        available=deployment_status.get("availableReplicas", 0), pending=pending,
        able_to_scale=_condition(status, "AbleToScale"), scaling_active=_condition(status, "ScalingActive"),
        scaling_limited=_condition(status, "ScalingLimited"), cpu_target_pct=cpu_target,
        cpu_current_pct=cpu_current, restart_count=restart_count,
    )
