"""Observed status of the Deploy stage.

NL2Infra never applies manifests. A person merges the pull request, Argo CD syncs the manifests
repository, and this module only *reads* what happened: the pull request (GitHub), the Argo CD
Application, and the live Kubernetes objects. A run is `deployed` only when all of these hold:

1. the pull request that belongs to this request is merged;
2. Argo CD reports `Synced` at exactly that merge commit;
3. every workload in the merged files is fully rolled out and ready;
4. every Service has ready endpoints, and each HTTP port answers 200 through the Service.

A merge alone never counts. If something cannot be observed, the status is not upgraded.
"""
import json
import logging
import os
import re
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import yaml

logger = logging.getLogger(__name__)

# Statuses that are final: the watcher stops looking at the run.
TERMINAL = frozenset({"deployed", "failed", "declined", "superseded", "simulated", "not_published"})

APPLICATIONS = {"dev": "nl2infra-dev", "staging": "nl2infra-staging", "production": "nl2infra-production"}
MANUAL_ENVIRONMENTS = frozenset({"production"})
WORKLOADS = ("Deployment", "StatefulSet", "DaemonSet", "Job")
REVIEW_POLL_SECONDS = 30

_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")


class ObservationError(RuntimeError):
    """GitHub or the cluster could not be read. Says nothing about the deployment itself."""


def is_simulated(pr_url) -> bool:
    """True for the mock pull request that MOCK_MODE records."""
    if not isinstance(pr_url, str):
        return False
    parsed = urlparse(pr_url)
    return parsed.hostname == "github.com" and parsed.path.startswith("/mock/repo/")


def is_trackable(pr_url) -> bool:
    if not isinstance(pr_url, str) or is_simulated(pr_url):
        return False
    return pr_url.startswith("https://github.com/")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timeout_seconds() -> int:
    try:
        return max(30, int(os.environ.get("NL2INFRA_DEPLOY_TIMEOUT_SECONDS", "600")))
    except ValueError:
        return 600


# ------------------------------------------------------------------- cluster reads

class KubectlCluster:
    """Read-only cluster access through kubectl, plus one HTTP probe.

    The probe runs `wget` inside the fixed `probe` Deployment (infra/cloud/probe.yaml), so the
    request goes through cluster DNS and the Service, not through the API server's proxy.
    """

    def __init__(self, kubectl: str = "kubectl", probe_namespace: Optional[str] = None):
        self.kubectl = kubectl
        self.probe_namespace = probe_namespace or os.environ.get("NL2INFRA_PROBE_NAMESPACE", "nl2infra-probe")

    def _run(self, args: list, timeout: int = 20) -> subprocess.CompletedProcess:
        try:
            return subprocess.run([self.kubectl, *args], capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ObservationError(f"kubectl did not answer: {type(error).__name__}") from error

    def get(self, resource: str, namespace: str, name: str) -> Optional[dict]:
        """The object, or None when the API server says it does not exist."""
        if not _NAME.match(name or "") or not _NAME.match(namespace or ""):
            raise ObservationError("invalid resource name")
        proc = self._run(["get", resource, name, "-n", namespace, "-o", "json", "--request-timeout=10s"])
        if proc.returncode != 0:
            if "(NotFound)" in (proc.stderr or ""):
                return None
            raise ObservationError((proc.stderr or "kubectl get failed").strip()[:300])
        try:
            return json.loads(proc.stdout)
        except ValueError as error:
            raise ObservationError("kubectl returned invalid JSON") from error

    def endpoint_slices(self, namespace: str, service: str) -> list:
        if not _NAME.match(service or "") or not _NAME.match(namespace or ""):
            raise ObservationError("invalid resource name")
        proc = self._run(["get", "endpointslices", "-n", namespace, "-l",
                          f"kubernetes.io/service-name={service}", "-o", "json", "--request-timeout=10s"])
        if proc.returncode != 0:
            raise ObservationError((proc.stderr or "kubectl get failed").strip()[:300])
        try:
            return json.loads(proc.stdout).get("items", [])
        except ValueError as error:
            raise ObservationError("kubectl returned invalid JSON") from error

    def http_status(self, namespace: str, service: str, port: int) -> Optional[int]:
        """HTTP status of GET / through the Service, or None when there was no HTTP answer."""
        if not _NAME.match(service or "") or not _NAME.match(namespace or ""):
            raise ObservationError("invalid resource name")
        url = f"http://{service}.{namespace}.svc.cluster.local:{int(port)}/"
        proc = self._run(["exec", "-n", self.probe_namespace, "deploy/probe", "--request-timeout=15s", "--",
                          "/bin/wget", "-S", "-T", "5", "-O", "/dev/null", url], timeout=25)
        codes = re.findall(r"HTTP/\d(?:\.\d)?\s+(\d{3})", proc.stderr or "")
        if codes:
            return int(codes[0])  # the Service's own answer, not a redirect target
        if proc.returncode != 0 and "wget:" not in (proc.stderr or ""):
            # kubectl itself failed (no probe pod, no permission): nothing was learned.
            raise ObservationError("HTTP probe could not run: " + (proc.stderr or "").strip()[:200])
        return None


# ------------------------------------------------------------------- evaluation

def _documents(files: dict, environment: str) -> list:
    documents = []
    for path, content in sorted(files.items()):
        try:
            loaded = list(yaml.safe_load_all(content))
        except yaml.YAMLError:
            raise ValueError(f"{path} in the merge commit is not valid YAML")
        for doc in loaded:
            if isinstance(doc, dict) and doc.get("kind") and (doc.get("metadata") or {}).get("name"):
                doc["metadata"].setdefault("namespace", environment)
                documents.append(doc)
    return documents


def _images(template: dict) -> list:
    return sorted(str(c.get("image")) for c in ((template or {}).get("spec") or {}).get("containers", []))


def _check(kind: str, name: str, namespace: str, ok: bool, detail: str) -> dict:
    return {"kind": kind, "name": name, "namespace": namespace, "ok": bool(ok), "detail": detail}


def _workload_check(cluster, doc: dict, autoscaled: bool) -> tuple:
    """(check, fatal_reason). fatal_reason is set when Kubernetes itself gave up."""
    kind, meta, spec = doc["kind"], doc["metadata"], doc.get("spec") or {}
    name, namespace = meta["name"], meta["namespace"]
    live = cluster.get(kind.lower(), namespace, name)
    if live is None:
        return _check(kind, name, namespace, False, "not found in the cluster"), None
    status, live_spec = live.get("status") or {}, live.get("spec") or {}
    if _images(live_spec.get("template")) != _images(spec.get("template")):
        return _check(kind, name, namespace, False, "live image differs from the merged file"), None
    if kind == "Job":
        failed = [c for c in status.get("conditions", []) if c.get("type") == "Failed" and c.get("status") == "True"]
        done = status.get("succeeded", 0) >= spec.get("completions", 1)
        reason = f"Job {name} failed: {failed[0].get('reason', 'unknown')}" if failed else None
        return _check(kind, name, namespace, done, f"{status.get('succeeded', 0)} succeeded"), reason

    generation = (live.get("metadata") or {}).get("generation", 0)
    observed = status.get("observedGeneration", 0) >= generation
    if kind == "DaemonSet":
        desired = status.get("desiredNumberScheduled", 0)
        ready = status.get("numberReady", 0)
        ok = observed and desired > 0 and ready == desired and status.get("updatedNumberScheduled", 0) == desired
        return _check(kind, name, namespace, ok, f"{ready}/{desired} ready"), None

    wanted = live_spec.get("replicas", 1) if autoscaled else spec.get("replicas", 1)
    ready = status.get("readyReplicas", 0)
    ok = (observed and wanted > 0 and live_spec.get("replicas", 1) == wanted
          and ready == wanted and status.get("replicas", 0) == wanted
          and status.get("updatedReplicas", 0) == wanted)
    if kind == "Deployment":
        ok = ok and status.get("availableReplicas", 0) == wanted
    stalled = [c for c in status.get("conditions", [])
               if c.get("type") == "Progressing" and c.get("reason") == "ProgressDeadlineExceeded"]
    reason = f"Deployment {name} exceeded its progress deadline" if stalled and observed else None
    return _check(kind, name, namespace, ok, f"{ready}/{wanted} replicas ready"), reason


def _is_http_port(port: dict) -> bool:
    return port.get("protocol", "TCP") == "TCP" and (
        port.get("port") in (80, 8080) or str(port.get("name", "")).startswith("http"))


def _service_checks(cluster, doc: dict) -> list:
    meta, spec = doc["metadata"], doc.get("spec") or {}
    name, namespace = meta["name"], meta["namespace"]
    live = cluster.get("service", namespace, name)
    if live is None:
        return [_check("Service", name, namespace, False, "not found in the cluster")]
    if not spec.get("selector"):
        return [_check("Service", name, namespace, True, "no selector: endpoints are not checked")]
    ready = set()
    for piece in cluster.endpoint_slices(namespace, name):
        for endpoint in piece.get("endpoints") or []:
            if (endpoint.get("conditions") or {}).get("ready"):
                ready.update(endpoint.get("addresses") or [])
    checks = [_check("Endpoints", name, namespace, bool(ready), f"{len(ready)} ready")]
    for port in spec.get("ports") or []:
        if not _is_http_port(port):
            checks.append(_check("Port", name, namespace, True, f"port {port.get('port')} is not HTTP: not probed"))
            continue
        if not ready:
            checks.append(_check("HTTP", name, namespace, False, f"port {port.get('port')} not probed: no ready endpoint"))
            continue
        code = cluster.http_status(namespace, name, port.get("port"))
        text = f"GET port {port.get('port')} returned {code}" if code else f"no HTTP answer on port {port.get('port')}"
        checks.append(_check("HTTP", name, namespace, code == 200, text))
    return checks


def evaluate(request_id: str, pull: dict, cluster, now: Optional[datetime] = None,
             timeout: Optional[int] = None) -> dict:
    """The deploy record for one pull request, from observed state only.

    `pull` is GitOpsService.pull_request_status(). Raises ObservationError if the cluster
    cannot be read.
    """
    now = now or _utc_now()
    timeout = timeout or _timeout_seconds()
    record = {
        "pr_number": pull.get("number"), "pr_state": pull.get("state"),
        "merge_commit": None, "environment": pull.get("environment"),
        "checked_at": now.isoformat(timespec="seconds"), "checks": [],
    }

    def result(status: str, detail: str) -> dict:
        record.update(status=status, detail=detail)
        return record

    if pull.get("head_ref") != f"nl2infra/{request_id}":
        return result("failed", "The pull request branch does not belong to this request")
    if pull.get("base_ref") != pull.get("default_branch"):
        return result("failed", "The pull request does not target the branch that Argo CD syncs")
    if not pull.get("merged"):
        if pull.get("state") == "closed":
            return result("declined", "The pull request was closed without merging. Nothing was deployed.")
        return result("waiting_review", "Waiting for a person to review and merge the pull request")

    sha = pull.get("merge_commit_sha")
    record["merge_commit"] = sha
    record["merged_at"] = pull.get("merged_at")
    environment = pull.get("environment")
    if environment not in APPLICATIONS:
        return result("failed", "The pull request does not change exactly one known environment folder")
    manual = environment in MANUAL_ENVIRONMENTS
    try:
        merged_at = datetime.fromisoformat(str(pull.get("merged_at")))
        if merged_at.tzinfo is None:
            merged_at = merged_at.replace(tzinfo=timezone.utc)
    except ValueError:
        raise ObservationError("GitHub did not report when the pull request was merged")
    expired = (now - merged_at).total_seconds() > timeout

    def waiting(detail: str) -> dict:
        if manual:
            return result("awaiting_manual_sync", f"Merged. {environment} is synced by hand. {detail}")
        if expired:
            return result("failed", f"Rollout timed out after {timeout} s: {detail}")
        return result("syncing", detail)

    try:
        documents = _documents(pull.get("files") or {}, environment)
    except ValueError as error:
        return result("failed", str(error))
    if not documents:
        return result("failed", "The merge commit holds no Kubernetes resources for this request")

    name = APPLICATIONS[environment]
    application = cluster.get("applications.argoproj.io", "argocd", name)
    if application is None:
        return waiting(f"Argo CD application {name} was not found")
    status = application.get("status") or {}
    sync = status.get("sync") or {}
    operation = status.get("operationState") or {}
    revision = sync.get("revision")
    operation_revision = ((operation.get("syncResult") or {}).get("revision")
                          or ((operation.get("operation") or {}).get("sync") or {}).get("revision"))
    record["argo"] = {
        "application": name, "sync_status": sync.get("status"), "revision": revision,
        "operation_phase": operation.get("phase"), "operation_revision": operation_revision,
        "health": (status.get("health") or {}).get("status"),
    }
    if operation.get("phase") in ("Failed", "Error") and operation_revision == sha:
        return result("failed", f"Argo CD sync failed: {str(operation.get('message', 'no message'))[:300]}")
    if revision != sha:
        if sha in [entry.get("revision") for entry in status.get("history") or []]:
            return result("superseded", "A later commit was synced before this one could be verified")
        return waiting(f"Waiting for Argo CD to sync merge commit {str(sha)[:12]} "
                       f"(it reports {str(revision)[:12]})")
    if sync.get("status") != "Synced" or operation.get("phase") == "Running":
        errors = [c.get("message", "") for c in status.get("conditions") or [] if "Error" in str(c.get("type"))]
        return waiting(errors[0][:300] if errors else f"Argo CD reports {sync.get('status') or 'no sync status'}")

    synced = {(r.get("kind"), r.get("namespace") or "", r.get("name")): r.get("status")
              for r in status.get("resources") or []}
    autoscaled = {((d.get("spec") or {}).get("scaleTargetRef") or {}).get("name")
                  for d in documents if d["kind"] == "HorizontalPodAutoscaler"}
    checks, fatal = [], None
    for doc in documents:
        kind, meta = doc["kind"], doc["metadata"]
        key = (kind, "" if kind == "Namespace" else meta["namespace"], meta["name"])
        in_sync = synced.get(key) == "Synced"
        checks.append(_check(kind, meta["name"], meta["namespace"], in_sync,
                             "synced by Argo CD" if in_sync else "not reported Synced by Argo CD"))
        if kind in WORKLOADS:
            check, reason = _workload_check(cluster, doc, meta["name"] in autoscaled)
            checks.append(check)
            fatal = fatal or reason
        elif kind == "Service":
            checks.extend(_service_checks(cluster, doc))
    record["checks"] = checks
    if fatal:
        return result("failed", fatal)
    pending = [f"{c['kind']}/{c['name']}: {c['detail']}" for c in checks if not c["ok"]]
    if pending:
        return waiting("; ".join(pending)[:500])
    return result("deployed", f"Argo CD synced {str(sha)[:12]} and every check passed")


# ------------------------------------------------------------------- persistence and watching

def view(run: dict) -> Optional[dict]:
    """What the dashboard should show for a saved or live run."""
    if is_simulated(run.get("pr_url")):
        return {"status": "simulated",
                "detail": "Simulated run: the pull request is a mock. Nothing was merged or deployed."}
    return run.get("deploy")


def refresh(request_id: str, run_dir: str, gitops, cluster) -> Optional[dict]:
    """Observes one run and saves the result. Terminal records are never changed."""
    from notifications import slack, emit_event_safe

    run = slack._read_run(request_id, run_dir)
    if not run or run.get("status") != "passed" or not is_trackable(run.get("pr_url")):
        return None
    previous = run.get("deploy") or {}
    if previous.get("status") in TERMINAL:
        return previous
    try:
        pull = gitops.pull_request_status(run["pr_url"], sorted(run.get("files") or {}))
        record = evaluate(request_id, pull, cluster)
    except Exception as error:
        # Keep the last observed status; say that it could not be refreshed.
        message = str(error) if isinstance(error, ObservationError) else type(error).__name__
        record = dict(previous or {"status": "unknown", "detail": "Deployment status is not available yet"})
        record["observation_error"] = message[:300]
        record["checked_at"] = _utc_now().isoformat(timespec="seconds")

    path = Path(run_dir) / f"{request_id}.json"
    with slack._action_lock, slack._run_file_lock(path):
        fresh = slack._read_run(request_id, run_dir)
        if not fresh:
            return None
        stored = fresh.get("deploy") or {}
        if stored.get("status") in TERMINAL:
            return stored
        fresh["deploy"] = record
        slack._write_run(path, fresh)

    status = record.get("status")
    if status != previous.get("status") and status in ("deployed", "failed"):
        emit_event_safe("deploy_succeeded" if status == "deployed" else "deploy_failed",
                        "success" if status == "deployed" else "critical", request_id,
                        f"Deployment {status}: {record.get('detail', '')}"[:400],
                        metadata={"pr_url": run["pr_url"], "merge_commit": record.get("merge_commit")})
    return record


def _checked_recently(deploy: dict, now: datetime) -> bool:
    """An unreviewed pull request is polled less often, to stay inside GitHub's rate limit."""
    if deploy.get("status") != "waiting_review":
        return False
    try:
        checked = datetime.fromisoformat(deploy["checked_at"])
        return (now - checked).total_seconds() < REVIEW_POLL_SECONDS
    except (KeyError, TypeError, ValueError):
        return False


def pending_runs(run_dir: str, now: Optional[datetime] = None) -> list:
    """Runs with a real pull request whose outcome is still open and due for a look."""
    now = now or _utc_now()
    pending = []
    for path in Path(run_dir).glob("*.json"):
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(run, dict) or run.get("status") != "passed" or not is_trackable(run.get("pr_url")):
            continue
        deploy = run.get("deploy") or {}
        if deploy.get("status") not in TERMINAL and not _checked_recently(deploy, now):
            pending.append(run.get("request_id") or path.stem)
    return pending


def enabled() -> bool:
    return os.environ.get("NL2INFRA_DEPLOY_OBSERVER", "false").lower() == "true"


def start_watcher(run_dir: str, gitops_factory: Callable, cluster=None,
                  stop: Optional[threading.Event] = None) -> threading.Thread:
    """Refreshes unfinished runs in the background. Failures never reach the web app."""
    cluster = cluster or KubectlCluster()
    stop = stop or threading.Event()
    try:
        interval = max(5, int(os.environ.get("NL2INFRA_DEPLOY_POLL_SECONDS", "10")))
    except ValueError:
        interval = 10

    def watch():
        while not stop.is_set():
            try:
                for request_id in pending_runs(run_dir):
                    refresh(request_id, run_dir, gitops_factory(), cluster)
            except Exception:
                logger.warning("Deploy status refresh failed", exc_info=True)
            stop.wait(interval)

    thread = threading.Thread(target=watch, name="nl2infra-deploy-watcher", daemon=True)
    thread.start()
    return thread
