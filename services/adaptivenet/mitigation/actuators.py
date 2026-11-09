"""The only code that changes anything. Every method is narrow and read-back capable.

* GatewayActuator   - local simulator route weights (X-Lab-Token required)
* KubernetesActuator - bounded replica changes for one allowlisted Deployment/HPA per region
AWS Route 53 / CloudFront are intentionally NOT touched here.
"""
from __future__ import annotations
import os
from typing import Protocol

class Actuator(Protocol):
    def read_route(self) -> int: ...
    def set_route(self, region_a_percent: int) -> None: ...
    def read_replicas(self, region: str) -> int | None: ...       # the value set_replicas controls
    def read_capacity(self, region: str) -> int | None: ...       # replicas currently desired
    def read_ready_replicas(self, region: str) -> int: ...        # pods actually serving
    def set_replicas(self, region: str, replicas: int) -> None: ...
    @property
    def can_scale(self) -> bool: ...


class GatewayActuator:
    """Routes through the local gateway; scaling is delegated to an optional scaler."""
    def __init__(self, base_url: str | None = None, token: str | None = None,
                 scaler: "KubernetesActuator | None" = None, client=None):
        import httpx
        self.base = (base_url or os.getenv("GATEWAY_URL", "http://127.0.0.1:8080")).rstrip("/")
        self.token = token if token is not None else os.getenv("LAB_TOKEN")
        self.scaler = scaler
        self.client = client or httpx.Client(timeout=5, trust_env=False)

    def _call(self, method, path, body=None):
        if not self.token:
            raise RuntimeError("LAB_TOKEN is not set; the gateway requires it even to read route state")
        r = self.client.request(method, self.base + path, json=body, headers={"X-Lab-Token": self.token})
        r.raise_for_status()
        return r.json()

    @property
    def can_scale(self) -> bool:
        return self.scaler is not None

    def read_route(self) -> int:
        return int(self._call("GET", "/admin/state")["region_a_percent"])

    def set_route(self, region_a_percent: int) -> None:
        if isinstance(region_a_percent, bool) or not 0 <= region_a_percent <= 100:
            raise ValueError("route weight out of range")
        self._call("PUT", "/admin/routes", {"region_a_percent": region_a_percent})

    def read_replicas(self, region):
        return self.scaler.read_replicas(region) if self.scaler else None

    def read_capacity(self, region):
        return self.scaler.read_capacity(region) if self.scaler else None

    def read_ready_replicas(self, region):
        return self.scaler.read_ready_replicas(region) if self.scaler else 0

    def set_replicas(self, region, replicas):
        if not self.scaler:
            raise RuntimeError("no scale adapter configured")
        self.scaler.set_replicas(region, replicas)


class KubernetesActuator:
    """Scales one allowlisted workload per region.

    With an HPA the HPA owns `replicas`, so we raise `minReplicas` (and `maxReplicas`
    if needed) instead of fighting it; without one we patch Deployment scale.
    targets = {"region-a": {"namespace": "demo", "deployment": "demo-a", "hpa": "demo-a"}, ...}
    """
    HARD_MAX = 20

    def __init__(self, targets: dict[str, dict], apps_api=None, autoscaling_api=None):
        if apps_api is None or autoscaling_api is None:
            from kubernetes import client, config  # optional "cloud" extra
            config.load_kube_config() if not os.getenv("KUBERNETES_SERVICE_HOST") else config.load_incluster_config()
            apps_api = apps_api or client.AppsV1Api()
            autoscaling_api = autoscaling_api or client.AutoscalingV2Api()
        self.targets, self.apps, self.hpa = targets, apps_api, autoscaling_api
        self._raised_max: dict[str, int] = {}  # original maxReplicas, restored on rollback

    can_scale = True

    def _t(self, region):
        if region not in self.targets:
            raise ValueError("region not allowlisted for scaling")
        return self.targets[region]

    def read_replicas(self, region):
        t = self._t(region)
        if t.get("hpa"):  # HPA owns replicas; minReplicas is what we set, read back and roll back
            minimum = self.hpa.read_namespaced_horizontal_pod_autoscaler(t["hpa"], t["namespace"]).spec.min_replicas
            return 1 if minimum is None else int(minimum)  # Kubernetes defaults an unset minReplicas to 1
        return self.read_capacity(region)

    def read_capacity(self, region):
        """Replicas the Deployment currently wants; with an HPA this can be well above minReplicas."""
        t = self._t(region)
        return int(self.apps.read_namespaced_deployment_scale(t["deployment"], t["namespace"]).spec.replicas or 0)

    def read_ready_replicas(self, region):
        t = self._t(region)
        return int(self.apps.read_namespaced_deployment_status(t["deployment"], t["namespace"]).status.ready_replicas or 0)

    def set_replicas(self, region, replicas):
        t = self._t(region)
        if isinstance(replicas, bool) or not 1 <= replicas <= self.HARD_MAX:
            raise ValueError("replica count out of bounds")
        if t.get("hpa"):
            h = self.hpa.read_namespaced_horizontal_pod_autoscaler(t["hpa"], t["namespace"])
            patch = {"spec": {"minReplicas": replicas}}
            original_max = self._raised_max.get(region)
            if h.spec.max_replicas < replicas:
                self._raised_max.setdefault(region, h.spec.max_replicas)
                patch["spec"]["maxReplicas"] = replicas
            elif original_max is not None and replicas <= original_max:
                patch["spec"]["maxReplicas"] = self._raised_max.pop(region)
            self.hpa.patch_namespaced_horizontal_pod_autoscaler(t["hpa"], t["namespace"], patch)
        else:
            self.apps.patch_namespaced_deployment_scale(t["deployment"], t["namespace"], {"spec": {"replicas": replicas}})
