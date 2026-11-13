"""One-replica EKS HPA minimum changes with journal and atomic preconditions."""
from __future__ import annotations
import json
import math
import time

from .emulated_hpa import EmulatedScaleAction
from .journal import ActionJournal
from .kubernetes_hpa import KubernetesHpaReader, KubernetesReadError
from .scaling import recommend_capacity

ANNOTATION = "adaptivenet.io/capacity-action"

class LiveHpa:
    backend = "eks"

    def __init__(self, connection, journal: ActionJournal, *, readiness_timeout_sec=120):
        if not math.isfinite(readiness_timeout_sec) or readiness_timeout_sec <= 0:
            raise ValueError("invalid readiness timeout")
        self.connection, self.journal = connection, journal
        self.target, self.region = connection.target, connection.region
        self._reader = KubernetesHpaReader({self.region: self.target}, readers={self.target.cluster: connection})
        self._timeout = readiness_timeout_sec
        self._ready = {}
        self.last_observation = None

    @property
    def _path(self):
        return f"/apis/autoscaling/v2/namespaces/{self.target.namespace}/horizontalpodautoscalers/{self.target.hpa}"

    def observe(self):
        result = self._reader.assess(self.region)
        if result.observation is None:
            raise ValueError(result.recommendation.reason)
        self.last_observation = result.observation
        return result.observation

    def assess(self, region, *, now, observation=None):
        if observation is None:
            observation = self.observe()
            now = time.time()
        return recommend_capacity(region, allowlist={self.region: self.target}, observation=observation, now=now)

    def request_capacity(self, action_id, *, replicas, now):
        # SafetyController must durably reserve the exact action before any write.
        pending = self.journal.pending()
        expected = pending.expected if pending else None
        if (pending is None or pending.action_id != action_id or not expected
                or expected.get("region") != self.region or expected.get("replicas") != replicas
                or expected.get("target") != self.target.__dict__):
            raise ValueError("matching_controller_journal_required")
        try:
            obj = self.connection(self._path)
            old = obj["metadata"].get("annotations", {}).get(ANNOTATION)
            if old:
                previous = json.loads(old)
                if previous.get("action_id") == action_id:
                    if previous.get("replicas") != replicas:
                        raise ValueError("action_id_target_mismatch")
                    return self.poll_action(action_id, now=now)
            observation = self.observe()
            now = time.time()
            recommendation = recommend_capacity(self.region, allowlist={self.region: self.target}, observation=observation, now=now)
            if (type(replicas) is not int or recommendation.requested_replicas != replicas
                    or observation.current != pending.before.get("current")
                    or observation.min_replicas != pending.before.get("min_replicas")
                    or observation.max_replicas != pending.before.get("max_replicas")):
                raise ValueError("capacity_changed_or_not_eligible")
            if (type(observation.cpu_current_pct) is not int or type(observation.cpu_target_pct) is not int
                    or observation.cpu_current_pct < observation.cpu_target_pct):
                raise ValueError("live_cpu_does_not_support_scale_out")
            if obj["spec"]["minReplicas"] != observation.min_replicas or obj["spec"]["maxReplicas"] != observation.max_replicas:
                raise ValueError("hpa_changed_during_read")
            record = json.dumps({"action_id": action_id, "replicas": replicas,
                                 "requested_at": now, "original_min": observation.min_replicas}, allow_nan=False)
            patch = [
                {"op": "test", "path": "/metadata/resourceVersion", "value": obj["metadata"]["resourceVersion"]},
                {"op": "test", "path": "/spec/scaleTargetRef", "value": obj["spec"]["scaleTargetRef"]},
                {"op": "test", "path": "/spec/minReplicas", "value": observation.min_replicas},
                {"op": "test", "path": "/spec/maxReplicas", "value": observation.max_replicas},
                {"op": "replace", "path": "/spec/minReplicas", "value": replicas},
            ]
            if obj["metadata"].get("annotations") is None:
                patch.append({"op": "add", "path": "/metadata/annotations", "value": {}})
            patch.append({"op": "add", "path": "/metadata/annotations/adaptivenet.io~1capacity-action", "value": record})
            self.connection.patch_hpa(patch)
            return EmulatedScaleAction(action_id, replicas, now, "requested")
        except (KubernetesReadError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("live_hpa_request_failed_or_outcome_unknown") from exc

    def poll_action(self, action_id, *, now):
        try:
            obj = self.connection(self._path)
            record = json.loads(obj["metadata"].get("annotations", {}).get(ANNOTATION, "null"))
            if not record or record.get("action_id") != action_id:
                return None
            replicas, started = record["replicas"], record["requested_at"]
            if type(replicas) is not int or type(started) not in (int, float) or not math.isfinite(started) or now < started:
                raise ValueError("invalid_live_action_record")
            observation = self.observe()
            if observation.min_replicas != replicas:
                raise ValueError("live_hpa_minimum_changed")
            state, ready_at = "pending", None
            if (observation.current == observation.desired == observation.ready == observation.available == replicas
                    and observation.pending == 0):
                ready_at = self._ready.setdefault(action_id, now)
                state = "ready"
            elif now >= started + self._timeout:
                state = "timed_out"
            return EmulatedScaleAction(action_id, replicas, started, state, ready_at)
        except (KubernetesReadError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("live_hpa_outcome_unknown") from exc
