"""Gauntlet unit tests: plan conformance and tool-crash handling. No external tools needed."""
import json
import os
import stat

import pytest

from contracts import Plan, Resource, Violation
from src.nl2infra.validators import Validators, TOOL_CRASH, is_tool_crash, parse_files

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")
IMAGE = "nginxinc/nginx-unprivileged:1.27-alpine"


def load_fixture():
    with open(os.path.join(FIXTURES, "sample_files.json")) as f:
        files = json.load(f)
    with open(os.path.join(FIXTURES, "sample_plan.json")) as f:
        plan = Plan(**json.load(f))
    return files, plan


def rule_ids(violations):
    return sorted(v.rule_id for v in violations)


def deployment(name="web", namespace="dev", image="nginx:1.25", replicas=None, extra=""):
    replica_line = f"  replicas: {replicas}\n" if replicas is not None else ""
    return (
        "apiVersion: apps/v1\nkind: Deployment\n"
        f"metadata:\n  name: {name}\n  namespace: {namespace}\n"
        f"spec:\n{replica_line}"
        "  selector:\n    matchLabels:\n      app: web\n"
        "  template:\n    metadata:\n      labels:\n        app: web\n"
        f"    spec:\n      containers:\n        - name: web\n          image: {image}\n{extra}"
    )


SERVICE = "apiVersion: v1\nkind: Service\nmetadata:\n  name: web-svc\n  namespace: dev\nspec:\n  ports:\n    - port: 80\n"

PLAN = Plan(resources=[
    Resource(type="Deployment", name="web", namespace="dev", spec={"image": "nginx:1.25", "replicas": 3}),
    Resource(type="Service", name="web-svc", namespace="dev", spec={"port": 80}),
])


# ---------------------------------------------------------------- plan conformance

def test_plan_conformance_pass():
    files = {"deployment.yaml": deployment(replicas=3), "service.yaml": SERVICE}
    assert Validators()._plan_conformance(files, PLAN) == []


def test_plan_conformance_fixture_passes():
    files, plan = load_fixture()
    assert Validators()._plan_conformance(files, plan) == []


def test_plan_conformance_kind_is_case_insensitive_on_the_plan_side():
    plan = Plan(resources=[Resource(type="service", name="web-svc", namespace="dev", spec={})])
    assert Validators()._plan_conformance({"service.yaml": SERVICE}, plan) == []


def test_plan_conformance_missing_resource():
    violations = Validators()._plan_conformance({"deployment.yaml": deployment(replicas=3)}, PLAN)
    assert rule_ids(violations) == ["PLAN_CONFORMANCE_MISSING"]
    assert violations[0].tool == "plan"
    assert "web-svc" in violations[0].message


def test_plan_conformance_namespace_mismatch():
    files = {"deployment.yaml": deployment(namespace="production", replicas=3), "service.yaml": SERVICE}
    violations = Validators()._plan_conformance(files, PLAN)
    assert rule_ids(violations) == ["PLAN_CONFORMANCE_NAMESPACE"]
    assert violations[0].file == "deployment.yaml"
    assert violations[0].line == 5


def test_plan_conformance_missing_namespace_means_default():
    files = {"service.yaml": "apiVersion: v1\nkind: Service\nmetadata:\n  name: web-svc\n"}
    plan = Plan(resources=[Resource(type="Service", name="web-svc", namespace="dev", spec={})])
    assert rule_ids(Validators()._plan_conformance(files, plan)) == ["PLAN_CONFORMANCE_NAMESPACE"]


def test_plan_conformance_cluster_scoped_kind_has_no_namespace():
    files = {"namespace.yaml": "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: dev\n"}
    plan = Plan(resources=[Resource(type="Namespace", name="dev", namespace="", spec={})])
    assert Validators()._plan_conformance(files, plan) == []


@pytest.mark.parametrize("image", [
    "nginx:latest",                 # tag changed
    "nginx:1.26",                   # version changed
    "nginx@sha256:" + "6c5e" * 16,  # digest invented to satisfy CKV_K8S_43
    "docker.io/library/nginx:1.25",  # same image, different string
])
def test_plan_conformance_image_must_equal_the_plan(image):
    files = {"deployment.yaml": deployment(image=image, replicas=3), "service.yaml": SERVICE}
    violations = Validators()._plan_conformance(files, PLAN)
    assert rule_ids(violations) == ["PLAN_CONFORMANCE_IMAGE"]
    assert violations[0].file == "deployment.yaml"
    assert violations[0].line == 18
    assert "nginx:1.25" in violations[0].message


def test_plan_conformance_added_container_is_an_image_mismatch():
    extra = "        - name: sidecar\n          image: busybox:1.36\n"
    files = {"deployment.yaml": deployment(replicas=3, extra=extra), "service.yaml": SERVICE}
    assert rule_ids(Validators()._plan_conformance(files, PLAN)) == ["PLAN_CONFORMANCE_IMAGE"]


def test_plan_conformance_cronjob_image():
    cronjob = (
        "apiVersion: batch/v1\nkind: CronJob\nmetadata:\n  name: backup\n  namespace: dev\n"
        "spec:\n  schedule: '0 2 * * *'\n  jobTemplate:\n    spec:\n      template:\n        spec:\n"
        "          restartPolicy: OnFailure\n          containers:\n            - name: backup\n              image: {}\n"
    )
    plan = Plan(resources=[Resource(type="CronJob", name="backup", namespace="dev", spec={"image": "alpine:3.20"})])
    assert Validators()._plan_conformance({"cronjob.yaml": cronjob.format("alpine:3.20")}, plan) == []
    assert rule_ids(Validators()._plan_conformance({"cronjob.yaml": cronjob.format("alpine:3.19")}, plan)) == [
        "PLAN_CONFORMANCE_IMAGE"
    ]


def test_plan_conformance_replica_mismatch():
    files = {"deployment.yaml": deployment(replicas=1), "service.yaml": SERVICE}
    violations = Validators()._plan_conformance(files, PLAN)
    assert rule_ids(violations) == ["PLAN_CONFORMANCE_REPLICAS"]
    assert violations[0].line == 7


def test_plan_conformance_missing_replicas_means_one():
    files = {"deployment.yaml": deployment(), "service.yaml": SERVICE}
    assert rule_ids(Validators()._plan_conformance(files, PLAN)) == ["PLAN_CONFORMANCE_REPLICAS"]
    plan = Plan(resources=[Resource(type="Deployment", name="web", namespace="dev", spec={"image": "nginx:1.25"})])
    assert Validators()._plan_conformance({"deployment.yaml": deployment()}, plan) == []


def test_plan_conformance_unplanned_resource():
    crb = (
        "apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRoleBinding\nmetadata:\n  name: admin-all\n"
        "roleRef:\n  apiGroup: rbac.authorization.k8s.io\n  kind: ClusterRole\n  name: cluster-admin\n"
    )
    files = {"deployment.yaml": deployment(replicas=3), "service.yaml": SERVICE, "crb.yaml": crb}
    violations = Validators()._plan_conformance(files, PLAN)
    assert rule_ids(violations) == ["PLAN_CONFORMANCE_UNPLANNED"]
    assert violations[0].file == "crb.yaml"
    assert violations[0].resource == "ClusterRoleBinding/admin-all"


def test_plan_conformance_unplanned_resource_in_the_same_file():
    files = {"all.yaml": deployment(replicas=3) + "---\n" + SERVICE + "---\n" + SERVICE.replace("web-svc", "extra")}
    violations = Validators()._plan_conformance(files, PLAN)
    assert rule_ids(violations) == ["PLAN_CONFORMANCE_UNPLANNED"]
    assert violations[0].resource == "Service/extra"


def test_plan_conformance_duplicate_resource():
    files = {"deployment.yaml": deployment(replicas=3), "service.yaml": SERVICE, "service2.yaml": SERVICE}
    assert rule_ids(Validators()._plan_conformance(files, PLAN)) == ["PLAN_CONFORMANCE_DUPLICATE"]


def test_plan_conformance_empty_file():
    files = {"deployment.yaml": "", "service.yaml": SERVICE}
    assert rule_ids(Validators()._plan_conformance(files, PLAN)) == ["PLAN_CONFORMANCE_MISSING"]


def test_plan_conformance_invalid_yaml():
    files = {"deployment.yaml": "kind: Deployment\nmetadata:\n  name: [web\n", "service.yaml": SERVICE}
    violations = Validators()._plan_conformance(files, PLAN)
    assert rule_ids(violations) == ["PLAN_CONFORMANCE_MISSING", "YAML_SYNTAX_ERROR"]


def test_document_without_kind_is_reported_and_not_scanned():
    resources, violations, scannable = parse_files({"a.yaml": "foo: bar\n", "service.yaml": SERVICE})
    assert rule_ids(violations) == ["PLAN_CONFORMANCE_INVALID_DOC"]
    assert scannable == ["service.yaml"]
    assert [r.label for r in resources] == ["Service/web-svc"]


def test_validate_with_nothing_to_scan_is_never_a_pass(monkeypatch):
    validator = Validators()
    called = []
    monkeypatch.setattr(validator, "_run_checkov", lambda *a, **k: called.append("checkov") or [])
    violations = validator.validate({"deployment.yaml": "", "service.yaml": "# nothing\n"}, PLAN)
    assert called == []
    assert rule_ids(violations) == ["PLAN_CONFORMANCE_MISSING", "PLAN_CONFORMANCE_MISSING"]


# ---------------------------------------------------------------- tool crashes

def fake_tool(tmp_path, name, stdout="", exit_code=0, stderr=""):
    """A real executable that prints fixed output, to drive the validators through subprocess."""
    path = tmp_path / name
    (tmp_path / f"{name}.out").write_text(stdout)
    (tmp_path / f"{name}.err").write_text(stderr)
    path.write_text(f'#!/bin/sh\ncat "{path}.out"\ncat "{path}.err" >&2\nexit {exit_code}\n')
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@pytest.fixture
def scan_dir(tmp_path):
    target = tmp_path / "scan"
    target.mkdir()
    (target / "deployment.yaml").write_text(deployment(replicas=3))
    return str(target)


def assert_crash(violations, tool):
    assert len(violations) == 1
    assert violations[0].tool == tool
    assert violations[0].rule_id == TOOL_CRASH
    assert violations[0].severity == "CRITICAL"
    assert is_tool_crash(violations)


def test_is_tool_crash():
    assert not is_tool_crash([])
    assert not is_tool_crash([Violation(tool="checkov", rule_id="CKV_K8S_8", severity="MEDIUM", message="x")])


@pytest.mark.parametrize("tool,method,args", [
    ("checkov", "_run_checkov", ()),
    ("opa", "_run_conftest", ("junior_dev",)),
    ("dry-run", "_run_kubectl_dry_run", ()),
])
def test_missing_binary_is_a_tool_crash(monkeypatch, scan_dir, tool, method, args):
    monkeypatch.setattr("src.nl2infra.validators._resolve_binary", lambda name: None)
    validator = Validators()
    assert_crash(getattr(validator, method)(scan_dir, *args), tool)


CHECKOV_OK = json.dumps({
    "check_type": "kubernetes",
    "results": {"failed_checks": []},
    "summary": {"passed": 3, "failed": 0, "skipped": 0, "parsing_errors": 0, "resource_count": 1, "checkov_version": "x"},
})
CHECKOV_SUMMARY_ONLY = json.dumps(
    {"passed": 0, "failed": 0, "skipped": 0, "parsing_errors": 0, "resource_count": 0, "checkov_version": "x"}
)


@pytest.mark.parametrize("stdout,exit_code", [
    ("", 0),                                    # exit 0, empty output
    ("Scanning... done", 0),                    # exit 0, not JSON
    ("{}", 0),                                  # JSON without results or summary
    ('{"results": {"failed_checks": []}}', 0),  # results without a summary
    ('{"results": {}, "summary": {"failed": 0, "checkov_version": "x"}}', 0),
    (json.dumps({"results": {"failed_checks": []},
                 "summary": {"failed": 2, "parsing_errors": 0, "checkov_version": "x"}}), 0),  # counts disagree
    (json.dumps({"results": {"failed_checks": []},
                 "summary": {"failed": 0, "parsing_errors": 1, "checkov_version": "x"}}), 0),  # file skipped
    ("Fatal error: Segmentation fault (core dumped)", 137),
    (CHECKOV_OK, 2),                            # valid JSON but a failing exit code
])
def test_checkov_bad_output_is_a_tool_crash(tmp_path, scan_dir, stdout, exit_code):
    validator = Validators()
    validator.checkov_bin = fake_tool(tmp_path, "checkov", stdout, exit_code)
    assert_crash(validator._run_checkov(scan_dir), "checkov")


def test_checkov_wellformed_output_is_accepted(tmp_path, scan_dir):
    validator = Validators()
    validator.checkov_bin = fake_tool(tmp_path, "checkov", CHECKOV_OK)
    assert validator._run_checkov(scan_dir, expect_resources=True) == []


def test_checkov_summary_only_is_a_pass_only_without_workloads(tmp_path, scan_dir):
    validator = Validators()
    validator.checkov_bin = fake_tool(tmp_path, "checkov", CHECKOV_SUMMARY_ONLY)
    assert validator._run_checkov(scan_dir, expect_resources=False) == []
    assert_crash(validator._run_checkov(scan_dir, expect_resources=True), "checkov")


def test_checkov_timeout_is_a_tool_crash(monkeypatch, scan_dir):
    import subprocess

    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="checkov", timeout=120)

    monkeypatch.setattr("subprocess.run", boom)
    validator = Validators()
    validator.checkov_bin = "/usr/bin/checkov"
    assert_crash(validator._run_checkov(scan_dir), "checkov")


def test_checkov_missing_config_is_a_tool_crash(tmp_path, scan_dir):
    validator = Validators(policies_dir=str(tmp_path / "nowhere"))
    validator.checkov_bin = fake_tool(tmp_path, "checkov", CHECKOV_OK)
    assert_crash(validator._run_checkov(scan_dir), "checkov")


def conftest_json(scan_dir, **fields):
    return json.dumps([{"filename": os.path.join(scan_dir, "deployment.yaml"), "namespace": "main", **fields}])


@pytest.mark.parametrize("stdout,exit_code,stderr", [
    ("", 0, ""),                                  # exit 0, empty output
    ("ok", 0, ""),                                # exit 0, not JSON
    ("{}", 0, ""),                                # JSON, but not a result list
    ("[]", 0, ""),                                # no file was evaluated
    ("", 1, "rego_parse_error: 'if' keyword is required before rule body"),
    ("", 2, "panic"),
])
def test_conftest_bad_output_is_a_tool_crash(tmp_path, scan_dir, stdout, exit_code, stderr):
    validator = Validators()
    validator.conftest_bin = fake_tool(tmp_path, "conftest", stdout, exit_code, stderr)
    assert_crash(validator._run_conftest(scan_dir, "junior_dev"), "opa")


def test_conftest_zero_rules_evaluated_is_a_tool_crash(tmp_path, scan_dir):
    validator = Validators()
    validator.conftest_bin = fake_tool(tmp_path, "conftest", conftest_json(scan_dir, successes=0))
    assert_crash(validator._run_conftest(scan_dir, "junior_dev"), "opa")


def test_conftest_exit_one_without_failures_is_a_tool_crash(tmp_path, scan_dir):
    validator = Validators()
    validator.conftest_bin = fake_tool(tmp_path, "conftest", conftest_json(scan_dir, successes=5), exit_code=1)
    assert_crash(validator._run_conftest(scan_dir, "junior_dev"), "opa")


def test_conftest_wellformed_output_is_normalised(tmp_path, scan_dir):
    failure = {"msg": "[OPA_NO_ROOT] Container 'web' must not run as root",
               "metadata": {"rule_id": "OPA_NO_ROOT", "resource": "Deployment/web"}}
    validator = Validators()
    validator.conftest_bin = fake_tool(
        tmp_path, "conftest", conftest_json(scan_dir, successes=4, failures=[failure]), exit_code=1
    )
    violations = validator._run_conftest(scan_dir, "junior_dev", lines={("deployment.yaml", "Deployment/web"): 1})
    assert len(violations) == 1
    v = violations[0]
    assert (v.tool, v.rule_id, v.severity, v.file, v.line, v.resource) == (
        "opa", "OPA_NO_ROOT", "HIGH", "deployment.yaml", 1, "Deployment/web"
    )
    assert v.message == "Container 'web' must not run as root"


def test_conftest_missing_policy_pack_is_a_tool_crash(tmp_path, scan_dir):
    (tmp_path / "policies" / "base").mkdir(parents=True)  # exists but has no rules
    validator = Validators(policies_dir=str(tmp_path / "policies"))
    validator.conftest_bin = fake_tool(tmp_path, "conftest", conftest_json(scan_dir, successes=5))
    assert_crash(validator._run_conftest(scan_dir, "junior_dev"), "opa")


def test_conftest_unknown_role_is_a_tool_crash(tmp_path, scan_dir):
    validator = Validators()
    validator.conftest_bin = fake_tool(tmp_path, "conftest", conftest_json(scan_dir, successes=5))
    assert_crash(validator._run_conftest(scan_dir, "hacker"), "opa")


def test_kubectl_exit_zero_without_dry_run_result_is_a_tool_crash(tmp_path, scan_dir):
    validator = Validators()
    validator.kubectl_bin = fake_tool(tmp_path, "kubectl", "")
    assert_crash(validator._run_kubectl_dry_run(scan_dir), "dry-run")


def test_validate_reports_each_crashed_tool(monkeypatch):
    monkeypatch.setattr("src.nl2infra.validators._resolve_binary", lambda name: None)
    files = {"deployment.yaml": deployment(replicas=3), "service.yaml": SERVICE}
    violations = Validators().validate(files, PLAN)
    assert sorted((v.tool, v.rule_id) for v in violations) == [
        ("checkov", TOOL_CRASH), ("dry-run", TOOL_CRASH), ("opa", TOOL_CRASH)
    ]
    assert is_tool_crash(violations)
