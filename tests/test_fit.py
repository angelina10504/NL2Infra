"""Tests for the 'fit' layer: requirement loading, must-have conformance, examples."""
import os

import pytest

import fit
from contracts import ProjectProfile, WorkloadType

ALL_TYPES = [t.value for t in WorkloadType]


# ---------------------------------------------------------------- data-driven requirements

@pytest.mark.parametrize("wt", ALL_TYPES)
def test_every_type_has_a_requirements_file_with_kinds_and_must_haves(wt):
    req = fit.load_requirements(wt)
    assert req["kinds"], f"{wt} must list at least one Kubernetes kind"
    assert req["must_have"], f"{wt} must list at least one must-have"
    for item in req["must_have"]:
        assert item["id"] and item["description"]


def test_expected_kinds_are_type_appropriate():
    assert "StatefulSet" in fit.expected_kinds(WorkloadType.database)
    assert "CronJob" in fit.expected_kinds(WorkloadType.cron_job)
    assert fit.expected_kinds(WorkloadType.one_time_job) == ["Job"]
    assert "Service" in fit.expected_kinds(WorkloadType.web_app)


def test_requirements_live_in_yaml_not_python():
    # The rules are data: the files exist on disk and Python has no hardcoded copy.
    for wt in ALL_TYPES:
        assert os.path.isfile(os.path.join(fit.REQUIREMENTS_DIR, f"{wt}.yaml"))


# ---------------------------------------------------------------- must-have conformance

GOOD_WEB = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
  namespace: dev
spec:
  template:
    spec:
      containers:
        - name: web
          image: nginx:1.27-alpine
          readinessProbe: {httpGet: {path: /, port: 8080}}
          livenessProbe: {httpGet: {path: /, port: 8080}}
          resources: {limits: {cpu: 100m, memory: 64Mi}}
"""

BARE_WEB = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
  namespace: dev
spec:
  template:
    spec:
      containers:
        - name: web
          image: nginx:1.27-alpine
"""


def test_fit_conformance_passes_a_complete_web_app():
    profile = ProjectProfile(workload_type="web_app", app_name="web")
    assert fit.fit_conformance({"d.yaml": GOOD_WEB}, profile) == []


def test_fit_conformance_flags_missing_must_haves():
    profile = ProjectProfile(workload_type="web_app", app_name="web")
    ids = {v.rule_id for v in fit.fit_conformance({"d.yaml": BARE_WEB}, profile)}
    assert ids == {"FIT_MUSTHAVE_RESOURCE_LIMITS", "FIT_MUSTHAVE_READINESS_PROBE", "FIT_MUSTHAVE_LIVENESS_PROBE"}


def test_fit_conformance_messages_are_actionable_and_fixable():
    profile = ProjectProfile(workload_type="web_app", app_name="web")
    v = next(v for v in fit.fit_conformance({"d.yaml": BARE_WEB}, profile) if v.rule_id == "FIT_MUSTHAVE_READINESS_PROBE")
    assert v.tool == "plan"            # tool='plan' => flows through the fix loop, never a TOOL_CRASH
    assert v.file == "d.yaml" and v.resource == "Deployment/web"
    assert "readinessProbe" in v.message


def test_fit_conformance_requires_a_volume_for_a_database():
    profile = ProjectProfile(workload_type="database", app_name="db", needs_storage=True)
    # A database rendered as a bare Deployment has no StatefulSet volume at all.
    ids = {v.rule_id for v in fit.fit_conformance({"d.yaml": BARE_WEB}, profile)}
    assert "FIT_MUSTHAVE_VOLUME_CLAIM" in ids


def test_fit_conformance_requires_a_secret_ref_when_the_profile_needs_one():
    profile = ProjectProfile(workload_type="web_app", app_name="web", secrets_needed=["DB_PASSWORD"])
    ids = {v.rule_id for v in fit.fit_conformance({"d.yaml": GOOD_WEB}, profile)}
    assert "FIT_MUSTHAVE_SECRET_REF" in ids


def test_fit_conformance_is_noop_without_a_profile():
    assert fit.fit_conformance({"d.yaml": BARE_WEB}, None) == []


# ---------------------------------------------------------------- examples / grounding

@pytest.mark.parametrize("wt", ALL_TYPES)
def test_every_type_ships_at_least_one_example(wt):
    assert fit.examples_for(wt).strip(), f"{wt} needs an example manifest under examples/{wt}/"


def test_grounding_block_names_the_type_and_must_haves():
    profile = ProjectProfile(workload_type="database", app_name="db")
    block = fit.grounding_block(profile)
    assert "database" in block and "StatefulSet" in block
    assert "volumeClaimTemplates" in block            # pulled from the example
    assert fit.grounding_block(None) == ""
