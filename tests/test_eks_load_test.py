import json
import pytest
from scripts.eks_load_test import job_manifest, restoration_patch
from adaptivenet.controller.live_hpa import ANNOTATION


def test_load_job_has_deadline_no_retries_bounded_resources_and_no_api_token():
    job=job_manifest("fixture-load", "trusted-image")
    spec=job["spec"]
    pod=spec["template"]["spec"]
    assert spec["activeDeadlineSeconds"] == 120
    assert spec["backoffLimit"] == 0
    assert spec["ttlSecondsAfterFinished"] == 300
    assert pod["automountServiceAccountToken"] is False
    container=pod["containers"][0]
    assert container["image"] == "trusted-image"
    assert container["resources"]["limits"] == {"cpu":"500m","memory":"128Mi"}
    assert "time.monotonic() + 90" in container["command"][-1]
    compile(container["command"][-1], "load_worker", "exec")


def owned_hpa():
    return {"metadata":{"resourceVersion":"27","annotations":{ANNOTATION:json.dumps({"action_id":"ours"})}},
            "spec":{"minReplicas":3,"maxReplicas":6}}


def test_cleanup_tests_ownership_and_resource_version_without_changing_maximum():
    patch=restoration_patch(owned_hpa(), "ours", 3, 2, 6)
    assert patch[0] == {"op":"test","path":"/metadata/resourceVersion","value":"27"}
    assert patch[-1] == {"op":"replace","path":"/spec/minReplicas","value":2}
    assert all(item["op"]=="test" for item in patch[:-1])


@pytest.mark.parametrize("change",["owner","minimum","maximum"])
def test_cleanup_refuses_concurrent_operator_changes(change):
    obj=owned_hpa()
    if change=="owner":
        obj["metadata"]["annotations"][ANNOTATION]=json.dumps({"action_id":"someone-else"})
    elif change=="minimum":
        obj["spec"]["minReplicas"]=4
    else:
        obj["spec"]["maxReplicas"]=5
    with pytest.raises(ValueError,match="manual_inspection_required"):
        restoration_patch(obj,"ours",3,2,6)
