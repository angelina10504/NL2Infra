"""Gauntlet tests against the real tools: Checkov, Conftest and a live cluster.

Nothing is mocked here. Tests marked `cluster` need `kubectl` pointed at a cluster
that has a `dev` namespace (the kind cluster from `make cluster`). A test whose tool
is absent is skipped with the reason shown; run pytest with -rs to list them.
"""
import copy
import os
import subprocess

import pytest

from contracts import Plan, Resource, Violation
from src.nl2infra.validators import Validators, TOOL_CRASH, ROLE_PACKS, is_tool_crash, _resolve_binary
from tests.test_validators import load_fixture, rule_ids

POLICIES = os.path.join(os.path.dirname(__file__), "..", "policies")

BAD_DEPLOYMENT = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: bad
  namespace: dev
spec:
  replicas: 1
  selector:
    matchLabels:
      app: bad
  template:
    metadata:
      labels:
        app: bad
    spec:
      containers:
        - name: bad
          image: nginx:latest
          env:
            - name: DB_PASSWORD
              value: hunter2
"""
BAD_PLAN = Plan(resources=[
    Resource(type="Deployment", name="bad", namespace="dev", spec={"image": "nginx:latest", "replicas": 1})
])


def _cluster_ready() -> bool:
    kubectl = _resolve_binary("kubectl")
    if not kubectl:
        return False
    try:
        probe = subprocess.run(
            [kubectl, "get", "namespace", "dev", "--request-timeout=5s"], capture_output=True, timeout=15
        )
    except Exception:
        return False
    return probe.returncode == 0


needs_checkov = pytest.mark.skipif(not _resolve_binary("checkov"), reason="checkov is not installed")
needs_conftest = pytest.mark.skipif(not _resolve_binary("conftest"), reason="conftest is not installed")
needs_cluster = pytest.mark.skipif(not _cluster_ready(), reason="no reachable cluster with a 'dev' namespace")
pytestmark = pytest.mark.real_tools


@pytest.fixture
def scan(tmp_path):
    """Writes files into a directory the way validate() does, and returns its path."""
    def write(files):
        for name, content in files.items():
            (tmp_path / name).write_text(content)
        return str(tmp_path)
    return write


# ---------------------------------------------------------------- Rego

@needs_conftest
def test_rego_unit_tests_pass():
    """Every rule has a passing and a failing Rego test; conftest verify must be green."""
    proc = subprocess.run(
        [_resolve_binary("conftest"), "verify", "--policy", POLICIES, "--no-color"], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "0 failures" in proc.stdout


@needs_conftest
def test_every_rule_has_a_pass_and_a_fail_test():
    import re
    rules, tests = set(), ""
    for root, _, names in os.walk(POLICIES):
        for name in names:
            text = open(os.path.join(root, name)).read()
            if name.endswith("_test.rego"):
                tests += text
            elif name.endswith(".rego"):
                rules |= set(re.findall(r'result\("([A-Z_]+)"', text))
    assert len(rules) == 10
    for rule in rules:
        bodies = re.findall(r"(test_\w+) if \{(.*?)\n\}", tests, re.S)
        using = [name for name, body in bodies if f'"{rule}"' in body]
        assert any("_pass" in name for name in using), f"{rule} has no pass test"
        assert any("_fail" in name for name in using), f"{rule} has no fail test"


@needs_conftest
@pytest.mark.parametrize("role", sorted(ROLE_PACKS))
def test_conftest_loads_the_policies_for_every_role(scan, role):
    files, _ = load_fixture()
    violations = Validators()._run_conftest(scan(files), role)
    assert violations == []


@needs_conftest
@pytest.mark.parametrize("role,expected", [
    ("junior_dev", ["OPA_NO_PLAINTEXT_SECRET", "OPA_NO_ROOT", "OPA_REQUIRE_CPU_LIMIT", "OPA_REQUIRE_MEM_LIMIT"]),
    ("senior_dev", ["OPA_DISALLOW_LATEST_TAG", "OPA_NO_PLAINTEXT_SECRET", "OPA_NO_ROOT", "OPA_REQUIRE_CPU_LIMIT",
                    "OPA_REQUIRE_MEM_LIMIT", "OPA_REQUIRE_READINESS_PROBE"]),
    ("platform_admin", ["OPA_DISALLOW_LATEST_TAG", "OPA_NO_PLAINTEXT_SECRET", "OPA_NO_ROOT", "OPA_READONLY_ROOTFS",
                        "OPA_REQUIRE_CPU_LIMIT", "OPA_REQUIRE_MEM_LIMIT", "OPA_REQUIRE_READINESS_PROBE"]),
])
def test_conftest_finds_violations_per_role(scan, role, expected):
    violations = Validators()._run_conftest(
        scan({"deployment.yaml": BAD_DEPLOYMENT}), role, lines={("deployment.yaml", "Deployment/bad"): 1}
    )
    assert rule_ids(violations) == expected
    for v in violations:
        assert (v.tool, v.file, v.line, v.resource, v.severity) == ("opa", "deployment.yaml", 1, "Deployment/bad", "HIGH")
        assert not v.message.startswith("[")
        Violation.model_validate(v.model_dump())


@needs_conftest
def test_conftest_checks_statefulsets_not_only_deployments(scan):
    statefulset = BAD_DEPLOYMENT.replace("kind: Deployment", "kind: StatefulSet")
    violations = Validators()._run_conftest(scan({"sts.yaml": statefulset}), "junior_dev")
    assert "OPA_NO_ROOT" in rule_ids(violations)


@needs_conftest
def test_conftest_with_old_rego_syntax_is_a_tool_crash(scan, tmp_path_factory):
    """The original failure: pre-1.0 Rego must surface as TOOL_CRASH, never as zero violations."""
    policies = tmp_path_factory.mktemp("policies")
    (policies / "base").mkdir()
    (policies / "base" / "policy.rego").write_text('package main\n\ndeny[msg] {\n    msg := "x"\n}\n')
    violations = Validators(policies_dir=str(policies))._run_conftest(scan({"d.yaml": BAD_DEPLOYMENT}), "junior_dev")
    assert rule_ids(violations) == [TOOL_CRASH]
    assert "rego_parse_error" in violations[0].message


# ---------------------------------------------------------------- Checkov

@needs_checkov
def test_checkov_clean_fixture_has_no_violations(scan):
    files, _ = load_fixture()
    assert Validators()._run_checkov(scan(files), expect_resources=True) == []


@needs_checkov
def test_checkov_finds_violations_and_never_asks_for_a_digest(scan):
    violations = Validators()._run_checkov(scan({"deployment.yaml": BAD_DEPLOYMENT}), expect_resources=True)
    found = set(rule_ids(violations))
    assert {"CKV_K8S_14", "CKV_K8S_23", "CKV_K8S_11", "CKV_K8S_13", "CKV_K8S_9"} <= found
    assert not {"CKV_K8S_43", "CKV2_K8S_6"} & found  # both skipped in policies/checkov.yaml
    assert TOOL_CRASH not in found
    for v in violations:
        assert (v.tool, v.file, v.resource) == ("checkov", "deployment.yaml", "Deployment.dev.bad")
        assert v.line == 1 and v.message
        Violation.model_validate(v.model_dump())


@needs_checkov
def test_checkov_without_the_skip_would_demand_a_digest(scan, tmp_path_factory):
    """Guards the reason for the skip: with no config, CKV_K8S_43 fires on a tagged image."""
    policies = tmp_path_factory.mktemp("policies")
    (policies / "checkov.yaml").write_text("skip-check: []\n")
    violations = Validators(policies_dir=str(policies))._run_checkov(scan({"deployment.yaml": BAD_DEPLOYMENT}))
    assert {"CKV_K8S_43", "CKV2_K8S_6"} <= set(rule_ids(violations))


@needs_checkov
def test_checkov_namespace_only_is_not_a_crash(scan):
    files = {"namespace.yaml": "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: dev\n"}
    assert Validators()._run_checkov(scan(files), expect_resources=False) == []


# ---------------------------------------------------------------- kubectl

@needs_cluster
def test_dry_run_accepts_the_clean_fixture(scan):
    files, _ = load_fixture()
    assert Validators()._run_kubectl_dry_run(scan(files)) == []


@needs_cluster
def test_dry_run_rejects_an_unknown_field(scan):
    broken = BAD_DEPLOYMENT.replace("          image: nginx:latest", "          image: nginx:latest\n          bogusField: 1")
    violations = Validators()._run_kubectl_dry_run(scan({"deployment.yaml": broken, "ok.yaml": load_fixture()[0]["service.yaml"]}))
    assert rule_ids(violations) == ["KUBECTL_DRY_RUN_FAILED"]
    v = violations[0]
    assert (v.tool, v.file, v.severity) == ("dry-run", "deployment.yaml", "HIGH")
    assert "bogusField" in v.message
    assert "/tmp" not in v.message and "pytest-" not in v.message


@needs_cluster
def test_dry_run_planned_namespace_missing_from_cluster_is_a_tool_crash(scan):
    files = {"deployment.yaml": BAD_DEPLOYMENT.replace("namespace: dev", "namespace: nl2infra-absent")}
    violations = Validators()._run_kubectl_dry_run(scan(files), planned_namespaces={"nl2infra-absent"})
    assert rule_ids(violations) == [TOOL_CRASH]
    assert "nl2infra-absent" in violations[0].message


@needs_cluster
def test_dry_run_unplanned_namespace_is_a_fixable_violation(scan):
    files = {"deployment.yaml": BAD_DEPLOYMENT.replace("namespace: dev", "namespace: nl2infra-absent")}
    violations = Validators()._run_kubectl_dry_run(scan(files), planned_namespaces={"dev"})
    assert rule_ids(violations) == ["KUBECTL_DRY_RUN_FAILED"]


@needs_cluster
def test_dry_run_accepts_a_namespace_created_by_the_same_files(scan):
    """A dry-run persists nothing, so resources in a namespace the files create are checked against 'default'."""
    namespace = "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: nl2infra-absent\n"
    configmap = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: a\n  namespace: nl2infra-absent\ndata:\n  k: v\n"
    validator = Validators()
    declared = {"nl2infra-absent"}
    assert validator._run_kubectl_dry_run(scan({"all.yaml": namespace + "---\n" + configmap}), declared_namespaces=declared) == []
    # the schema is still checked
    broken = configmap.replace("data:\n  k: v\n", "data:\n  k: v\nbogusField: 1\n")
    violations = validator._run_kubectl_dry_run(scan({"all.yaml": namespace + "---\n" + broken}), declared_namespaces=declared)
    assert rule_ids(violations) == ["KUBECTL_DRY_RUN_FAILED"] and "bogusField" in violations[0].message


def test_dry_run_unreachable_cluster_is_a_tool_crash(scan, tmp_path_factory, monkeypatch):
    """Real kubectl against a dead endpoint: an outage must never reach the fix loop."""
    if not _resolve_binary("kubectl"):
        pytest.skip("kubectl is not installed")
    kubeconfig = tmp_path_factory.mktemp("kube") / "config"
    kubeconfig.write_text(
        "apiVersion: v1\nkind: Config\ncurrent-context: dead\n"
        "clusters:\n  - name: dead\n    cluster:\n      server: https://127.0.0.1:1\n"
        "contexts:\n  - name: dead\n    context:\n      cluster: dead\n      user: dead\n"
        "users:\n  - name: dead\n    user: {}\n"
    )
    monkeypatch.setenv("KUBECONFIG", str(kubeconfig))
    violations = Validators()._run_kubectl_dry_run(scan({"deployment.yaml": BAD_DEPLOYMENT}))
    assert rule_ids(violations) == [TOOL_CRASH]
    assert violations[0].tool == "dry-run"
    assert "unreachable" in violations[0].message.lower()


# ---------------------------------------------------------------- full gauntlet

@needs_checkov
@needs_conftest
@needs_cluster
@pytest.mark.parametrize("role", sorted(ROLE_PACKS))
def test_gauntlet_clean_fixture_reaches_zero_violations(role):
    """Zero violations is reachable with a tagged image and no digest."""
    files, plan = load_fixture()
    assert "@sha256" not in "".join(files.values())
    assert Validators().validate(files, plan, role=role) == []


@needs_checkov
@needs_conftest
@needs_cluster
def test_gauntlet_zero_violations_without_a_network_policy():
    """A Deployment and Service alone can pass: no rule requires an unplanned resource."""
    files, plan = load_fixture()
    files = {k: v for k, v in files.items() if k != "networkpolicy.yaml"}
    plan = Plan(resources=[r for r in plan.resources if r.type != "NetworkPolicy"])
    assert Validators().validate(files, plan, role="platform_admin") == []


@needs_checkov
@needs_conftest
@needs_cluster
def test_gauntlet_bad_manifest_fails_in_checkov_and_opa():
    violations = Validators().validate({"deployment.yaml": BAD_DEPLOYMENT}, BAD_PLAN, role="senior_dev")
    assert not is_tool_crash(violations)
    assert {v.tool for v in violations} == {"checkov", "opa"}
    assert all(v.file == "deployment.yaml" for v in violations)


@needs_checkov
@needs_conftest
@needs_cluster
def test_gauntlet_rejects_an_invented_digest():
    """The known issue: an otherwise clean manifest whose image was swapped for a made-up digest."""
    files, plan = load_fixture()
    files = copy.deepcopy(files)
    files["deployment.yaml"] = files["deployment.yaml"].replace(
        "nginxinc/nginx-unprivileged:1.27-alpine", "nginxinc/nginx-unprivileged@sha256:" + "6c5e" * 16
    )
    violations = Validators().validate(files, plan, role="junior_dev")
    assert rule_ids(violations) == ["PLAN_CONFORMANCE_IMAGE"]
    assert violations[0].file == "deployment.yaml"


@needs_checkov
@needs_conftest
@needs_cluster
def test_gauntlet_rejects_an_unplanned_resource_and_changed_replicas():
    files, plan = load_fixture()
    files = copy.deepcopy(files)
    files["deployment.yaml"] = files["deployment.yaml"].replace("replicas: 2", "replicas: 1")
    files["configmap.yaml"] = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: extra\n  namespace: dev\ndata:\n  a: b\n"
    violations = Validators().validate(files, plan, role="junior_dev")
    assert rule_ids(violations) == ["PLAN_CONFORMANCE_REPLICAS", "PLAN_CONFORMANCE_UNPLANNED"]


@needs_checkov
@needs_conftest
@needs_cluster
def test_gauntlet_broken_yaml_is_a_violation_not_a_crash():
    """Conftest aborts on bad YAML and Checkov drops it silently; neither may decide the outcome."""
    files, plan = load_fixture()
    files = copy.deepcopy(files)
    files["service.yaml"] = "apiVersion: v1\nkind: Service\nmetadata:\n  name: [web-app-svc\n"
    violations = Validators().validate(files, plan, role="junior_dev")
    assert not is_tool_crash(violations)
    assert rule_ids(violations) == ["PLAN_CONFORMANCE_MISSING", "YAML_SYNTAX_ERROR"]
    assert [v.file for v in violations if v.rule_id == "YAML_SYNTAX_ERROR"] == ["service.yaml"]
