"""The Deploy stage reports only what was observed. A merge alone never means deployed."""
import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import yaml

import deploy_tracker as tracker
import server
from notifications import list_events, slack

SHA = "a" * 40
OTHER = "b" * 40
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
MOCK_URL = "https://github.com/mock/repo/pull/pr"
REAL_URL = "https://github.com/acme/manifests/pull/42"

DEPLOYMENT = {
    "apiVersion": "apps/v1", "kind": "Deployment",
    "metadata": {"name": "web", "namespace": "staging"},
    "spec": {"replicas": 2, "template": {"spec": {"containers": [{"name": "web", "image": "example/web:1.0"}]}}},
}
SERVICE = {
    "apiVersion": "v1", "kind": "Service", "metadata": {"name": "web", "namespace": "staging"},
    "spec": {"selector": {"app": "web"}, "ports": [{"port": 80, "targetPort": 8080}]},
}


def pull_request(**changes):
    pull = {
        "number": 42, "state": "closed", "merged": True, "head_ref": "nl2infra/pr",
        "base_ref": "main", "default_branch": "main", "merge_commit_sha": SHA,
        "merged_at": (NOW - timedelta(seconds=30)).isoformat(), "environment": "staging",
        "files": {"staging/deployment.yaml": yaml.safe_dump(DEPLOYMENT),
                  "staging/service.yaml": yaml.safe_dump(SERVICE)},
    }
    pull.update(changes)
    return pull


class FakeCluster:
    """A healthy rollout; each test breaks one thing."""

    def __init__(self):
        self.application = {"status": {
            "sync": {"status": "Synced", "revision": SHA},
            "operationState": {"phase": "Succeeded", "syncResult": {"revision": SHA}},
            "resources": [{"kind": k, "namespace": "staging", "name": "web", "status": "Synced"}
                          for k in ("Deployment", "Service")],
        }}
        self.deployment = copy.deepcopy(DEPLOYMENT)
        self.deployment["metadata"]["generation"] = 1
        self.deployment["status"] = {"observedGeneration": 1, "replicas": 2, "readyReplicas": 2,
                                     "updatedReplicas": 2, "availableReplicas": 2}
        self.service = copy.deepcopy(SERVICE)
        self.slices = [{"endpoints": [{"addresses": ["10.0.0.1"], "conditions": {"ready": True}},
                                      {"addresses": ["10.0.0.2"], "conditions": {"ready": True}}]}]
        self.code = 200
        self.probed = []

    def get(self, resource, namespace, name):
        return {"applications.argoproj.io": self.application, "deployment": self.deployment,
                "service": self.service}.get(resource)

    def endpoint_slices(self, namespace, service):
        return self.slices

    def http_status(self, namespace, service, port):
        self.probed.append((namespace, service, port))
        return self.code


def evaluate(cluster=None, pull=None, after=0):
    return tracker.evaluate("pr", pull or pull_request(), cluster or FakeCluster(),
                            now=NOW + timedelta(seconds=after), timeout=600)


def test_healthy_rollout_is_deployed_with_evidence():
    cluster = FakeCluster()
    record = evaluate(cluster)
    assert record["status"] == "deployed" and record["merge_commit"] == SHA
    assert record["argo"]["revision"] == SHA
    assert cluster.probed == [("staging", "web", 80)]
    assert all(check["ok"] for check in record["checks"])
    details = " ".join(check["detail"] for check in record["checks"])
    assert "2/2 replicas ready" in details and "2 ready" in details and "returned 200" in details


def test_open_pull_request_waits_and_closed_one_is_declined():
    cluster = SimpleNamespace()  # the cluster must not be consulted before a merge
    assert evaluate(cluster, pull_request(merged=False, state="open"))["status"] == "waiting_review"
    assert evaluate(cluster, pull_request(merged=False, state="closed"))["status"] == "declined"


@pytest.mark.parametrize("change", [{"head_ref": "nl2infra/other"}, {"base_ref": "side-branch"},
                                    {"environment": None}, {"files": {}}])
def test_pull_request_that_cannot_be_correlated_fails(change):
    assert evaluate(pull=pull_request(**change))["status"] == "failed"


def break_revision(c):
    c.application["status"]["sync"]["revision"] = OTHER


def break_sync(c):
    c.application["status"]["sync"]["status"] = "OutOfSync"


def break_running(c):
    c.application["status"]["operationState"]["phase"] = "Running"


def break_resource(c):
    c.application["status"]["resources"].pop()


def break_application(c):
    c.application = None


def break_replicas(c):
    c.deployment["status"].update(readyReplicas=1, availableReplicas=1)


def break_generation(c):
    c.deployment["status"]["observedGeneration"] = 0


def break_replica_count(c):
    c.deployment["spec"]["replicas"] = 1
    c.deployment["status"].update(replicas=1, readyReplicas=1, updatedReplicas=1, availableReplicas=1)


def break_old_pods(c):
    c.deployment["status"]["replicas"] = 3


def break_image(c):
    c.deployment["spec"]["template"]["spec"]["containers"][0]["image"] = "example/web:0.9"


def break_missing(c):
    c.deployment = None


def break_endpoints(c):
    c.slices = [{"endpoints": [{"addresses": ["10.0.0.1"], "conditions": {"ready": False}}]}]


def break_http(c):
    c.code = 503


def break_http_silent(c):
    c.code = None


BREAKS = [break_revision, break_sync, break_running, break_resource, break_application, break_replicas,
          break_generation, break_replica_count, break_old_pods, break_image, break_missing,
          break_endpoints, break_http, break_http_silent]


@pytest.mark.parametrize("breakage", BREAKS)
def test_merge_alone_is_never_deployed(breakage):
    cluster = FakeCluster()
    breakage(cluster)
    assert evaluate(cluster)["status"] == "syncing"
    late = evaluate(cluster, after=3600)
    assert late["status"] == "failed" and "timed out" in late["detail"]


def test_argo_sync_failure_for_this_commit_fails_at_once():
    cluster = FakeCluster()
    cluster.application["status"]["operationState"].update(phase="Failed", message="admission webhook denied")
    record = evaluate(cluster)
    assert record["status"] == "failed" and "admission webhook denied" in record["detail"]


def test_failure_of_an_older_sync_is_not_blamed_on_this_commit():
    cluster = FakeCluster()
    cluster.application["status"]["sync"]["revision"] = OTHER
    cluster.application["status"]["operationState"] = {"phase": "Failed", "syncResult": {"revision": OTHER}}
    assert evaluate(cluster)["status"] == "syncing"


def test_progress_deadline_fails_the_run():
    cluster = FakeCluster()
    cluster.deployment["status"].update(readyReplicas=0, availableReplicas=0, conditions=[
        {"type": "Progressing", "status": "False", "reason": "ProgressDeadlineExceeded"}])
    record = evaluate(cluster)
    assert record["status"] == "failed" and "progress deadline" in record["detail"]


def test_later_commit_synced_over_this_one_is_superseded():
    cluster = FakeCluster()
    cluster.application["status"]["sync"]["revision"] = OTHER
    cluster.application["status"]["history"] = [{"revision": SHA}, {"revision": OTHER}]
    assert evaluate(cluster)["status"] == "superseded"


def test_production_waits_for_a_manual_sync_without_timing_out():
    manifest = copy.deepcopy(DEPLOYMENT)
    manifest["metadata"]["namespace"] = "production"
    pull = pull_request(environment="production",
                        files={"production/deployment.yaml": yaml.safe_dump(manifest)})
    cluster = FakeCluster()
    cluster.application["status"]["sync"] = {"status": "OutOfSync", "revision": OTHER}
    cluster.application["status"]["operationState"] = {}
    assert evaluate(cluster, pull, after=3600)["status"] == "awaiting_manual_sync"


def kubectl_result(returncode=0, stdout="", stderr=""):
    return lambda args, timeout=20: SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_probe_goes_through_the_service_and_reads_the_status(monkeypatch):
    cluster = tracker.KubectlCluster(probe_namespace="probe-ns")
    seen = []

    def run(args, timeout=20):
        seen.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="  HTTP/1.1 200 OK\n")
    monkeypatch.setattr(cluster, "_run", run)
    assert cluster.http_status("staging", "web", 80) == 200
    assert "http://web.staging.svc.cluster.local:80/" in seen[0] and "probe-ns" in seen[0]
    monkeypatch.setattr(cluster, "_run", kubectl_result(
        1, stderr="  HTTP/1.1 503 Service Unavailable\nwget: server returned error: HTTP/1.1 503"))
    assert cluster.http_status("staging", "web", 80) == 503
    monkeypatch.setattr(cluster, "_run", kubectl_result(1, stderr="wget: can't connect to remote host"))
    assert cluster.http_status("staging", "web", 80) is None


def test_unreadable_cluster_is_an_observation_error_not_a_missing_object(monkeypatch):
    cluster = tracker.KubectlCluster()
    monkeypatch.setattr(cluster, "_run", kubectl_result(1, stderr='Error from server (NotFound): x not found'))
    assert cluster.get("deployment", "staging", "web") is None
    monkeypatch.setattr(cluster, "_run", kubectl_result(1, stderr="Unable to connect to the server"))
    with pytest.raises(tracker.ObservationError):
        cluster.get("deployment", "staging", "web")
    monkeypatch.setattr(cluster, "_run", kubectl_result(1, stderr="Error from server (NotFound): deployments.apps"))
    with pytest.raises(tracker.ObservationError):
        cluster.get("deployment", "staging", "--all")
    monkeypatch.setattr(cluster, "_run", kubectl_result(1, stderr="error: unable to forward"))
    with pytest.raises(tracker.ObservationError):
        cluster.http_status("staging", "web", 80)


def save(tmp_path, **changes):
    run = {"request_id": "pr", "status": "passed", "violations": [], "pr_url": REAL_URL,
           "files": {"deployment.yaml": "", "service.yaml": ""}, "deploy": {"status": "awaiting_merge"}}
    run.update(changes)
    path = tmp_path / "runs" / "pr.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(run))
    return path


def gitops(pull):
    calls = []

    def status(url, filenames):
        calls.append((url, filenames))
        return pull
    return SimpleNamespace(pull_request_status=status, calls=calls)


def test_refresh_saves_the_result_once_and_keeps_it(tmp_path):
    path = save(tmp_path)
    service = gitops(pull_request(merged_at=datetime.now(timezone.utc).isoformat()))
    assert tracker.refresh("pr", str(path.parent), service, FakeCluster())["status"] == "deployed"
    assert json.loads(path.read_text())["deploy"]["status"] == "deployed"
    assert service.calls == [(REAL_URL, ["deployment.yaml", "service.yaml"])]
    broken = FakeCluster()
    break_http(broken)
    assert tracker.refresh("pr", str(path.parent), service, broken)["status"] == "deployed"
    assert len(service.calls) == 1 and tracker.pending_runs(str(path.parent)) == []
    assert [e["type"] for e in list_events()].count("deploy_succeeded") == 1


def test_refresh_keeps_the_last_status_when_nothing_can_be_observed(tmp_path):
    path = save(tmp_path, deploy={"status": "syncing", "detail": "waiting"})

    def explode(url, filenames):
        raise RuntimeError("token=secret-value rejected")
    record = tracker.refresh("pr", str(path.parent), SimpleNamespace(pull_request_status=explode), FakeCluster())
    assert record["status"] == "syncing" and record["observation_error"] == "RuntimeError"
    assert "secret-value" not in path.read_text()
    assert tracker.pending_runs(str(path.parent)) == ["pr"]


@pytest.mark.parametrize("url", [MOCK_URL, "file:///tmp/manifests (Local GitOps simulated)", None])
def test_simulated_or_unpublished_runs_are_never_observed(url, tmp_path):
    path = save(tmp_path, pr_url=url)
    service = SimpleNamespace(pull_request_status=lambda *a: pytest.fail("must not call GitHub"))
    assert tracker.refresh("pr", str(path.parent), service, FakeCluster()) is None
    assert tracker.pending_runs(str(path.parent)) == []


def test_mock_run_is_shown_as_simulated_and_cannot_be_merged(team_client, monkeypatch, tmp_path):
    path = save(tmp_path, pr_url=MOCK_URL)
    monkeypatch.setattr(server, "_pipeline", SimpleNamespace(run_dir=str(path.parent)))
    monkeypatch.setattr(server, "_live_runs", {})
    monkeypatch.setattr(slack, "_gitops_service", lambda: pytest.fail("A mock PR must not reach GitHub"))
    client = team_client("admin", "1", "owner")
    assert client.get("/api/run/pr").json()["deploy"]["status"] == "simulated"
    assert client.get("/api/runs").json()[0]["deploy"]["status"] == "simulated"
    assert client.post("/api/pr/review", json={"request_id": "pr", "approved": True}).status_code == 409
    slack.review_from_dashboard("pr", True, "1", str(path.parent))
    assert "slack_approval" not in json.loads(path.read_text())


def test_saved_observation_replaces_the_older_live_snapshot(team_client, monkeypatch, tmp_path):
    path = save(tmp_path, deploy={"status": "deployed", "merge_commit": SHA})
    monkeypatch.setattr(server, "_pipeline", SimpleNamespace(run_dir=str(path.parent)))
    monkeypatch.setattr(server, "_live_runs", {"pr": {
        "request_id": "pr", "status": "passed", "pr_url": REAL_URL, "deploy": {"status": "awaiting_merge"}}})
    client = team_client("viewer", "2", "viewer")
    assert client.get("/api/run/pr").json()["deploy"]["status"] == "deployed"
    assert client.get("/api/runs").json()[0]["deploy"]["status"] == "deployed"


def test_unreviewed_pull_requests_are_polled_less_often(tmp_path):
    path = save(tmp_path, deploy={"status": "waiting_review", "checked_at": NOW.isoformat()})
    folder = str(path.parent)
    assert tracker.pending_runs(folder, now=NOW + timedelta(seconds=10)) == []
    assert tracker.pending_runs(folder, now=NOW + timedelta(seconds=31)) == ["pr"]


def test_watcher_is_off_by_default(monkeypatch):
    monkeypatch.delenv("NL2INFRA_DEPLOY_OBSERVER", raising=False)
    assert not tracker.enabled()


def test_app_starts_the_watcher_only_when_enabled(monkeypatch):
    from fastapi.testclient import TestClient
    started = []
    monkeypatch.setattr(tracker, "start_watcher", lambda *a, **k: started.append(k["stop"]))
    monkeypatch.delenv("NL2INFRA_DEPLOY_OBSERVER", raising=False)
    with TestClient(server.app):
        pass
    assert not started
    monkeypatch.setenv("NL2INFRA_DEPLOY_OBSERVER", "true")
    with TestClient(server.app):
        assert not started[0].is_set()
    assert started[0].is_set()


def test_dashboard_no_longer_hardcodes_the_deploy_stage():
    from pathlib import Path
    markup = (Path(server.__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")
    assert "NOT BUILT" not in markup and "Simulated: nothing was deployed" in markup


class FakeRepo:
    default_branch = "main"

    def __init__(self, files):
        self.files, self.refs = files, []

    def get_contents(self, path, ref):
        from github import UnknownObjectException
        self.refs.append(ref)
        if path not in self.files:
            raise UnknownObjectException(404, {}, {})
        return SimpleNamespace(decoded_content=self.files[path].encode())


def fake_pr(repo, merged, changed):
    return SimpleNamespace(
        number=42, state="closed" if merged else "open", merged=merged, merge_commit_sha=SHA,
        merged_at=NOW if merged else None, head=SimpleNamespace(ref="nl2infra/pr"),
        base=SimpleNamespace(ref="main", repo=repo),
        get_files=lambda: [SimpleNamespace(filename=name, status="modified") for name in changed])


def test_pull_request_status_reads_files_at_the_merge_commit(monkeypatch):
    from gitops import GitOpsService
    repo = FakeRepo({"staging/deployment.yaml": "d", "staging/service.yaml": "s"})
    service = GitOpsService(token="x", repo_name="acme/manifests")
    monkeypatch.setattr(service, "_pull_request",
                        lambda url: fake_pr(repo, True, ["staging/deployment.yaml"]))
    status = service.pull_request_status(REAL_URL, ["deployment.yaml", "service.yaml", "removed.yaml"])
    assert status["environment"] == "staging" and status["merge_commit_sha"] == SHA
    assert status["files"] == {"staging/deployment.yaml": "d", "staging/service.yaml": "s"}
    assert set(repo.refs) == {SHA}


def test_pull_request_status_ignores_merge_sha_of_open_prs_and_mixed_folders(monkeypatch):
    from gitops import GitOpsService
    service = GitOpsService(token="x", repo_name="acme/manifests")
    monkeypatch.setattr(service, "_pull_request", lambda url: fake_pr(FakeRepo({}), False, []))
    status = service.pull_request_status(REAL_URL)
    assert not status["merged"] and status["merge_commit_sha"] is None
    monkeypatch.setattr(service, "_pull_request", lambda url: fake_pr(
        FakeRepo({}), True, ["staging/a.yaml", "production/a.yaml"]))
    assert service.pull_request_status(REAL_URL)["environment"] is None
