import pytest
from contracts import Resource, Plan, Violation, RunState, ProjectProfile, WorkloadType

def test_resource_model():
    res = Resource(type="Deployment", name="web", namespace="default", spec={"image": "nginx:1.25", "replicas": 2})
    assert res.type == "Deployment"
    assert res.name == "web"
    assert res.namespace == "default"
    assert res.spec["replicas"] == 2

def test_plan_model():
    res1 = Resource(type="Deployment", name="web", namespace="default", spec={"image": "nginx"})
    res2 = Resource(type="Service", name="web-svc", namespace="default", spec={"port": 80})
    plan = Plan(resources=[res1, res2])
    assert len(plan.resources) == 2
    assert plan.resources[0].name == "web"

def test_violation_model():
    v = Violation(
        tool="checkov",
        rule_id="CKV_K8S_1",
        severity="HIGH",
        file="deployment.yaml",
        line=10,
        message="Container should not run as root",
        resource="Deployment.default.web"
    )
    assert v.tool == "checkov"
    assert v.rule_id == "CKV_K8S_1"
    assert v.severity == "HIGH"

def test_run_state_serialization():
    state = RunState(
        request_id="req-1234",
        user_role="junior_dev",
        user_prompt="deploy redis",
        model="mock"
    )
    assert state.status == "running"
    json_str = state.model_dump_json()
    assert "req-1234" in json_str
    reconstructed = RunState.model_validate_json(json_str)
    assert reconstructed.request_id == "req-1234"


def test_empty_plan_is_rejected():
    with pytest.raises(ValueError):
        Plan(resources=[])


@pytest.mark.parametrize("fields", [
    {"type": "Service", "name": "", "namespace": "dev", "spec": {}},
    {"type": "Service", "name": "   ", "namespace": "dev", "spec": {}},
    {"type": "", "name": "web", "namespace": "dev", "spec": {}},
    {"type": "Service", "name": "web", "namespace": "", "spec": {}},
    {"type": "Deployment", "name": "web", "namespace": "dev", "spec": {}},                # workload without an image
    {"type": "Deployment", "name": "web", "namespace": "dev", "spec": {"image": "nginx:1.25", "replicas": "3"}},
    {"type": "Deployment", "name": "web", "namespace": "dev", "spec": {"image": "nginx:1.25", "replicas": -1}},
])
def test_malformed_resource_is_rejected(fields):
    with pytest.raises(ValueError):
        Resource(**fields)


def test_planned_images_and_replicas():
    res = Resource(type="Deployment", name="web", namespace="dev", spec={
        "containers": [{"name": "web", "image": "nginx:1.25"}, {"name": "side", "image": "busybox:1.36"}]
    })
    assert res.planned_images() == ["nginx:1.25", "busybox:1.36"]
    assert res.planned_replicas() == 1
    assert Resource(type="Service", name="web", namespace="dev", spec={}).planned_replicas() is None
    assert Resource(type="Namespace", name="dev", namespace="", spec={}).namespace == ""


def test_unknown_severity_is_rejected():
    with pytest.raises(ValueError):
        Violation(tool="checkov", rule_id="X", severity="banana", message="m")


# --------------------------------------------------------------------------- ProjectProfile

def test_project_profile_defaults():
    # Only the required fields; everything else should get a safe default.
    p = ProjectProfile(workload_type="web_app", app_name="  shop  ")
    assert p.workload_type is WorkloadType.web_app
    assert p.app_name == "shop"            # trimmed
    assert p.image is None
    assert p.port is None
    assert p.needs_storage is False
    assert p.needs_public_access is False
    assert p.secrets_needed == [] and p.env_vars == [] and p.unknowns == []
    assert p.size == "small"


def test_project_profile_full_round_trip():
    p = ProjectProfile(
        workload_type="database", app_name="pg", image="postgres:16", port=5432,
        needs_storage=True, needs_public_access=False, secrets_needed=["pg-password"],
        env_vars=["POSTGRES_DB"], size="medium", unknowns=["port assumed 5432"],
    )
    restored = ProjectProfile.model_validate_json(p.model_dump_json())
    assert restored.workload_type is WorkloadType.database
    assert restored.needs_storage is True
    assert restored.secrets_needed == ["pg-password"]


@pytest.mark.parametrize("bad", [
    {"workload_type": "banana", "app_name": "x"},     # not a known type
    {"workload_type": "web_app", "app_name": ""},      # empty name
    {"workload_type": "web_app", "app_name": "x", "port": 70000},  # port out of range
    {"workload_type": "web_app", "app_name": "x", "size": "huge"}, # not a known size
])
def test_project_profile_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        ProjectProfile(**bad)
