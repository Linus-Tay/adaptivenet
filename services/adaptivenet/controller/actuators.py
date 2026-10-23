"""Actuators the controller can drive. Each returns a JSON-serialisable record.

  GatewayRouteActuator    local lab: PUT /admin/routes on the gateway (route weights)
  KubernetesScaleActuator in-cluster: patch a Deployment's scale subresource (bounded)
  ShadowActuator          records what would have happened; nothing changes (AWS path)

Nothing here touches Route 53, CloudFront, EC2 or other AWS APIs: a live AWS route
actuator still has to be built and authorized separately.
"""
from __future__ import annotations
import json
import os
from pathlib import Path

import httpx

SA = Path('/var/run/secrets/kubernetes.io/serviceaccount')


class ShadowActuator:
    scale_available = False

    async def set_route(self, region_a_percent: int) -> dict:
        return {'status': 'shadow', 'region_a_percent': region_a_percent}

    async def scale_out(self, region: str) -> dict:
        return {'status': 'shadow', 'region': region}


class GatewayRouteActuator:
    """Routes via the local gateway; scaling is journal-only unless a scaler is given."""
    def __init__(self, forward, scaler=None):
        self.forward = forward  # experiment_runner.forward: (method, path, body) -> JSONResponse
        self.scaler = scaler

    @property
    def scale_available(self) -> bool:
        return bool(self.scaler and self.scaler.scale_available)

    async def set_route(self, region_a_percent: int) -> dict:
        response = await self.forward('PUT', '/admin/routes', {'region_a_percent': region_a_percent})
        if response.status_code >= 400:
            return {'status': 'rejected', 'http_status': response.status_code}
        return {'status': 'applied', **json.loads(response.body)}

    async def scale_out(self, region: str) -> dict:
        if self.scaler is None:
            return {'status': 'not_configured', 'region': region, 'note': 'no scale adapter in this deployment'}
        return await self.scaler.scale_out(region)


class KubernetesScaleActuator:
    """Patches a Deployment's scale subresource using the pod's service account.

    RBAC (infra/k8s/control-api-rbac.yaml) allows only get/patch on deployments/scale
    in the controller's namespace. Replicas are capped by SCALE_MAX_REPLICAS.
    SCALE_TARGETS='region-a=demo-a,region-b=demo-b' maps regions to Deployments.
    """
    @staticmethod
    def targets() -> dict[str, str]:
        raw = os.getenv('SCALE_TARGETS', '').strip()
        return dict(pair.split('=', 1) for pair in raw.split(',') if '=' in pair) if raw else {}

    @property
    def scale_available(self) -> bool:
        return bool(self.targets()) and bool(os.getenv('KUBERNETES_SERVICE_HOST')) and (SA / 'token').exists()

    async def scale_out(self, region: str, step: int = 1) -> dict:
        deployment = self.targets().get(region)
        if not deployment or not self.scale_available:
            return {'status': 'not_configured', 'region': region}
        namespace = (SA / 'namespace').read_text().strip()
        url = (f"https://{os.environ['KUBERNETES_SERVICE_HOST']}:{os.getenv('KUBERNETES_SERVICE_PORT', '443')}"
               f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment}/scale")
        headers = {'Authorization': f"Bearer {(SA / 'token').read_text().strip()}"}
        async with httpx.AsyncClient(verify=str(SA / 'ca.crt'), timeout=5, trust_env=False) as client:
            current = (await client.get(url, headers=headers)).raise_for_status().json()['spec']['replicas']
            desired = min(current + step, int(os.getenv('SCALE_MAX_REPLICAS', '4')))
            if desired == current:
                return {'status': 'at_max', 'region': region, 'deployment': deployment, 'replicas': current}
            r = await client.patch(url, headers={**headers, 'Content-Type': 'application/merge-patch+json'},
                                   json={'spec': {'replicas': desired}})
            r.raise_for_status()
        return {'status': 'scaled', 'region': region, 'deployment': deployment, 'from': current, 'to': desired}

    async def set_route(self, region_a_percent: int) -> dict:
        return {'status': 'not_supported', 'note': 'Kubernetes actuator scales; routing needs the gateway or a cloud route actuator'}


class EmulatedScaleActuator:
    """LOCAL lab stand-in for Kubernetes replicas: PUT /admin/capacity on the regional
    workload divides the emulated overload across replicas. Journal records say
    `emulated` so nobody mistakes it for a cluster scale event."""
    scale_available = True

    def __init__(self, forward):
        self.forward = forward
        self.replicas = {'region-a': 1, 'region-b': 1}

    async def scale_out(self, region: str, step: int = 1) -> dict:
        from ..experiment_runner import region_base
        desired = min(self.replicas[region] + step, int(os.getenv('SCALE_MAX_REPLICAS', '4')))
        if desired == self.replicas[region]:
            return {'status': 'at_max', 'region': region, 'replicas': desired, 'emulated': True}
        response = await self.forward('PUT', '/admin/capacity', {'replicas': desired}, base=region_base(region))
        if response.status_code >= 400:
            return {'status': 'rejected', 'region': region, 'http_status': response.status_code, 'emulated': True}
        previous, self.replicas[region] = self.replicas[region], desired
        return {'status': 'scaled', 'region': region, 'from': previous, 'to': desired, 'emulated': True}

    def reset(self):
        self.replicas = {'region-a': 1, 'region-b': 1}


def scaler_from_env(forward):
    """SCALE_ACTUATOR=kubernetes | emulated | none (default none)."""
    choice = os.getenv('SCALE_ACTUATOR', 'none').lower()
    if choice == 'kubernetes':
        k8s = KubernetesScaleActuator()
        return k8s if k8s.scale_available else None
    if choice == 'emulated':
        return EmulatedScaleActuator(forward)
    return None
