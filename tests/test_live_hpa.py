from copy import deepcopy
from dataclasses import asdict
import json
from types import SimpleNamespace
import pytest
from test_kubernetes_hpa import api, TARGET, HPA, METRICS, NOW, timestamp
from adaptivenet.controller.journal import ActionJournal
from adaptivenet.controller.live_hpa import LiveHpa, ANNOTATION
from adaptivenet.controller.safety import SafetyController

@pytest.fixture
def live(api, tmp_path):
    reader, documents, _ = api
    documents[HPA]["metadata"]["resourceVersion"] = "1"
    writes = []
    class Connection:
        region = "region-a"
        target = TARGET
        def __call__(self, path, *, selector=None):
            return deepcopy(documents[path])
        def patch_hpa(self, patch):
            writes.append(patch)
            assert patch[0] == {"op": "test", "path": "/metadata/resourceVersion", "value": "1"}
            documents[HPA]["spec"]["minReplicas"] = patch[4]["value"]
            documents[HPA]["metadata"]["annotations"] = {ANNOTATION: patch[-1]["value"]}
    journal = ActionJournal(tmp_path / "actions.db")
    adapter = LiveHpa(Connection(), journal)
    return adapter, journal, documents, writes

def reserve(adapter, journal):
    observation = adapter.observe()
    assert journal.record_pending(action_id="a1", intent="scale:region-a:2->3", before=asdict(observation),
        expected={"region": "region-a", "target": asdict(TARGET), "replicas": 3})

def test_live_request_requires_controller_journal(live):
    adapter, _, _, writes = live
    with pytest.raises(ValueError, match="journal_required"):
        adapter.request_capacity("a1", replicas=3, now=NOW)
    assert writes == []

def test_live_request_only_raises_minimum_and_restart_does_not_reissue(live):
    adapter, journal, documents, writes = live
    reserve(adapter, journal)
    assert adapter.request_capacity("a1", replicas=3, now=NOW).state == "requested"
    restarted = LiveHpa(adapter.connection, journal)
    assert restarted.request_capacity("a1", replicas=3, now=NOW+1).state == "pending"
    assert len(writes) == 1
    assert documents[HPA]["spec"]["maxReplicas"] == 6
    assert all(p["op"] == "test" or p["path"] != "/spec/maxReplicas" for p in writes[0])
    assert json.loads(documents[HPA]["metadata"]["annotations"][ANNOTATION])["original_min"] == 2

@pytest.mark.parametrize("change", ["cpu", "bounds", "target"])
def test_changed_or_low_load_state_prevents_write(live, change):
    adapter, journal, documents, writes = live
    reserve(adapter, journal)
    if change == "cpu":
        documents[HPA]["status"]["currentMetrics"][0]["resource"]["current"]["averageUtilization"] = 2
    elif change == "bounds":
        documents[HPA]["spec"]["maxReplicas"] = 7
    else:
        documents[HPA]["spec"]["scaleTargetRef"]["name"] = "other"
    with pytest.raises(ValueError):
        adapter.request_capacity("a1", replicas=3, now=NOW)
    assert writes == []

def test_unknown_or_timed_out_action_does_not_retry(live):
    adapter, journal, _, writes = live
    reserve(adapter, journal)
    assert adapter.poll_action("a1", now=NOW) is None
    adapter.request_capacity("a1", replicas=3, now=NOW)
    assert adapter.poll_action("a1", now=NOW+121).state == "timed_out"
    assert len(writes) == 1

def test_controller_uses_live_adapter_after_three_distinct_metric_samples(live, monkeypatch):
    adapter, journal, documents, writes = live
    monkeypatch.setattr("adaptivenet.controller.safety.time.time", lambda: NOW)
    controller = SafetyController(journal, None, mode="eks", live_hpa=adapter)
    snapshot = {"snapshot_id": "features-test", "computed_at_unix": NOW,
                "ready_for_model": True, "regions": {}}
    proposal = {"contract_version": "adaptivenet.proposal.v1", "snapshot_id": "features-test",
                "action": "scale_out", "affected_region": "region-a", "confidence": .95,
                "evidence": ["test proposal"]}
    states = []
    for age in (3, 3, 2, 1):
        for item in documents[METRICS]["items"]:
            item["timestamp"] = timestamp(age)
        states.append(controller.step_scale(snapshot, proposal, compute_overloaded=True).state)
    assert states == ["qualifying", "qualifying", "qualifying", "action_pending"]
    assert len(writes) == 1
    assert journal.pending().action_id
    assert controller.step(snapshot, proposal, observed_at=NOW, source_degraded=True).reasons == ("eks_routing_not_implemented",)


def test_uncertain_patch_is_not_automatically_retried(live):
    from adaptivenet.controller.kubernetes_hpa import KubernetesReadError
    adapter, journal, _, writes = live
    reserve(adapter, journal)
    def fail(patch):
        writes.append(patch)
        raise KubernetesReadError("timeout")
    adapter.connection.patch_hpa = fail
    with pytest.raises(ValueError, match="outcome_unknown"):
        adapter.request_capacity("a1", replicas=3, now=NOW)
    assert len(writes) == 1
    assert journal.pending().action_id == "a1"

def test_live_ready_counts_are_not_reported_as_client_recovery(live):
    adapter, journal, documents, _ = live
    reserve(adapter, journal)
    adapter.request_capacity("a1", replicas=3, now=NOW)
    documents[HPA]["status"].update(currentReplicas=3, desiredReplicas=3)
    from test_kubernetes_hpa import DEPLOYMENT, PODS
    documents[DEPLOYMENT]["status"].update(readyReplicas=3, availableReplicas=3)
    pod = deepcopy(documents[PODS]["items"][0])
    pod["metadata"]["name"] = "catalog-c"
    documents[PODS]["items"].append(pod)
    metric = deepcopy(documents[METRICS]["items"][0])
    metric["metadata"]["name"] = "catalog-c"
    documents[METRICS]["items"].append(metric)
    action = adapter.poll_action("a1", now=NOW+5)
    assert action.state == "ready" and action.ready_at == NOW+5
    assert journal.pending().action_id == "a1"
    assert journal.outcome("a1") is None

def test_scale_cli_requires_original_inputs_before_connecting():
    from scripts.eks_controller import main
    with pytest.raises(SystemExit) as exc:
        main(["scale", "--site", "sg", "--kubeconfig", "unused"])
    assert exc.value.code == 2


def test_network_read_time_is_not_mistaken_for_future_observation(live, monkeypatch):
    adapter, journal, documents, writes = live
    clock = [NOW]
    monkeypatch.setattr("adaptivenet.controller.live_hpa.time.time", lambda: clock[0])
    original_read = adapter.connection.__class__.__call__
    def delayed_read(self, path, *, selector=None):
        clock[0] += .25
        return original_read(self, path, selector=selector)
    monkeypatch.setattr(adapter.connection.__class__, "__call__", delayed_read)
    # At idle the live adapter must still consider the reads fresh after network time.
    documents[HPA]["status"]["currentMetrics"][0]["resource"]["current"]["averageUtilization"] = 2
    controller = SafetyController(journal, None, mode="eks", live_hpa=adapter)
    snapshot = {"snapshot_id": "fixture", "computed_at_unix": NOW, "ready_for_model": True}
    proposal = {"contract_version": "adaptivenet.proposal.v1", "snapshot_id": "fixture",
                "action": "scale_out", "affected_region": "region-a", "confidence": .95,
                "evidence": ["fixture"]}
    result = controller.step_scale(snapshot, proposal, compute_overloaded=False)
    assert result.state == "observing"
    assert result.reasons == ("compute_evidence_missing_or_stale",)
    assert writes == []


@pytest.mark.parametrize("overloaded", [False, True])
def test_idle_controller_uses_one_observation_even_when_hpa_is_at_minimum(live, monkeypatch, overloaded):
    adapter, journal, documents, writes = live
    monkeypatch.setattr("adaptivenet.controller.safety.time.time", lambda: NOW)
    documents[HPA]["status"]["currentMetrics"][0]["resource"]["current"]["averageUtilization"] = 2
    documents[HPA]["status"]["conditions"][2]["status"] = "True"
    count = [0]
    original = adapter.observe
    def read():
        count[0] += 1
        return original()
    monkeypatch.setattr(adapter, "observe", read)
    controller = SafetyController(journal, None, mode="eks", live_hpa=adapter)
    snapshot = {"snapshot_id": "fixture", "computed_at_unix": NOW, "ready_for_model": True}
    proposal = {"contract_version": "adaptivenet.proposal.v1", "snapshot_id": "fixture",
                "action": "scale_out", "affected_region": "region-a", "confidence": .95,
                "evidence": ["fixture"]}
    result = controller.step_scale(snapshot, proposal, compute_overloaded=overloaded)
    assert result.state == "observing"
    assert result.reasons == ("compute_evidence_missing_or_stale",)
    assert count[0] == 1 and not writes and journal.pending() is None


def test_hpa_limit_remains_enforced_when_cpu_is_high(live, monkeypatch):
    adapter, journal, documents, writes = live
    monkeypatch.setattr("adaptivenet.controller.safety.time.time", lambda: NOW)
    documents[HPA]["status"]["conditions"][2]["status"] = "True"
    controller = SafetyController(journal, None, mode="eks", live_hpa=adapter)
    snapshot = {"snapshot_id": "fixture", "computed_at_unix": NOW, "ready_for_model": True}
    proposal = {"contract_version": "adaptivenet.proposal.v1", "snapshot_id": "fixture",
                "action": "scale_out", "affected_region": "region-a", "confidence": .95,
                "evidence": ["fixture"]}
    result = controller.step_scale(snapshot, proposal, compute_overloaded=True)
    assert result.reasons == ("hpa_scaling_limited_or_unknown",)
    assert not writes
