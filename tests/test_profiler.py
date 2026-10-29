"""Profiler tests: the deterministic scan, the scan-wins merge, and the gated graph node."""
import pytest

from contracts import ProjectProfile, WorkloadType
from profiler import build_profile, deterministic_scan, reconcile


# ---------------------------------------------------------------- deterministic scan

@pytest.mark.parametrize("prompt,expected", [
    ("Deploy an nginx web app to dev", WorkloadType.web_app),
    ("Stand up a REST api for orders", WorkloadType.api),
    ("Run a postgres database for the app", WorkloadType.database),
    ("A redis cache for sessions", WorkloadType.database),
    ("Create a cron job that runs nightly at midnight", WorkloadType.cron_job),
    ("A one-time migration job to seed data", WorkloadType.one_time_job),
    ("A celery background worker that consumes the queue", WorkloadType.queue_worker),
    ("Serve an ml model for inference with pytorch", WorkloadType.ml_service),
    ("Host a static site with our landing page", WorkloadType.static_site),
])
def test_scan_classifies_workload_type(prompt, expected):
    assert deterministic_scan(prompt).get("workload_type") == expected


def test_scan_reads_expose_port_from_a_dockerfile():
    facts = deterministic_scan("deploy my api", {"Dockerfile": "FROM python:3.12\nEXPOSE 9000\n"})
    assert facts["port"] == 9000


def test_scan_reads_compose_and_prompt_ports():
    assert deterministic_scan("x", {"docker-compose.yml": "services:\n  web:\n    ports:\n      - 8081:8082\n"})["port"] == 8082
    assert deterministic_scan("expose it on port 3000")["port"] == 3000


def test_scan_flags_storage_secrets_and_public_access():
    facts = deterministic_scan("A public postgres database; needs DB_PASSWORD and API_KEY")
    assert facts["workload_type"] == WorkloadType.database
    assert facts["needs_storage"] is True
    assert facts["needs_public_access"] is True
    assert facts["secrets_needed"] == ["API_KEY", "DB_PASSWORD"]


def test_scan_returns_little_when_nothing_is_evidenced():
    # A vague infra request: no type words, no port, no storage/secret signals.
    assert deterministic_scan("set something up") == {}


# ---------------------------------------------------------------- scan wins on merge

def test_scan_overrides_the_model_on_conflict():
    model_said = ProjectProfile(workload_type=WorkloadType.web_app, app_name="x", port=80)
    merged = reconcile(model_said, {"workload_type": WorkloadType.database, "port": 5432})
    assert merged.workload_type is WorkloadType.database      # scan wins
    assert merged.port == 5432
    assert any("workload_type" in u for u in merged.unknowns)  # the override is recorded, not silent


# ---------------------------------------------------------------- build_profile

class _StubLLM:
    def profile(self, prompt, files, hints, role="developer"):
        # Pretends the request is a web_app even though the scan says database.
        return ProjectProfile(workload_type=WorkloadType.web_app, app_name="svc", image="nginx:1.27")


def test_build_profile_lets_the_scan_win_over_the_llm():
    p = build_profile("a postgres database that persists data", None, _StubLLM())
    assert p.workload_type is WorkloadType.database
    assert p.needs_storage is True


def test_build_profile_falls_back_to_scan_when_no_llm():
    p = build_profile("deploy an api on port 9100", None, llm=None)
    assert p.workload_type is WorkloadType.api
    assert p.port == 9100
    assert isinstance(p, ProjectProfile)


def test_build_profile_records_unknowns_with_safe_defaults():
    # No image, no port stated for a web_app -> both recorded as unknowns, port defaulted.
    p = build_profile("deploy a web app", None, llm=None)
    assert p.workload_type is WorkloadType.web_app
    assert p.port == 8080                                   # safe default
    assert any("port" in u for u in p.unknowns)
    assert any("image" in u for u in p.unknowns)


def test_build_profile_never_raises_when_the_llm_errors():
    class Boom:
        def profile(self, *a, **k):
            raise RuntimeError("model down")
    p = build_profile("run a mysql database", None, Boom())
    assert p.workload_type is WorkloadType.database          # scan-only fallback still works


# ---------------------------------------------------------------- the gated graph node

def _mock_pipeline(tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_MODE", "true")
    from pipeline import Pipeline
    return Pipeline(auto_approve=True, run_dir=str(tmp_path))


def test_profiler_node_runs_only_when_project_aware(tmp_path, monkeypatch):
    pipeline = _mock_pipeline(tmp_path, monkeypatch)
    steps = []
    state = pipeline.start("Deploy a postgres database to dev", "developer", request_id="pa1",
                           project_aware=True, on_step=lambda name, s: steps.append(name))
    assert "profiler" in steps
    assert state.profile is not None
    assert state.profile.workload_type is WorkloadType.database


def test_profiler_node_is_skipped_by_default(tmp_path, monkeypatch):
    pipeline = _mock_pipeline(tmp_path, monkeypatch)
    steps = []
    state = pipeline.start("Deploy a server to the dev namespace", "developer", request_id="pa2",
                           on_step=lambda name, s: steps.append(name))
    assert "profiler" not in steps
    assert state.profile is None
