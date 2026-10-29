"""Project-Aware Generation — the profiler.

Works out WHAT KIND of project a request is, so the pipeline can plan and check the
Kubernetes objects that actually fit (a database is not a plain Deployment). Two parts:

  1. a deterministic scan (no AI) of the request text and any pasted files
     (Dockerfile, package.json, requirements.txt, docker-compose.yml). Predictable.
  2. an LLM pass that fills in the rest as JSON validated against ProjectProfile.

When the scan and the LLM disagree, the scan wins. Anything neither supplies lands in
`unknowns` with a safe default, so nothing is ever guessed silently.
"""
import re
from typing import Dict, List, Optional, Tuple

from contracts import ProjectProfile, WorkloadType

# Keyword -> workload type, checked in this order so the strongest signal wins. Data here
# is intentionally small and plain; the kinds and must-haves live in requirements/*.yaml.
_SCHEDULE_WORDS = ("cronjob", "cron ", "cron-", "schedule", "scheduled", "nightly", "hourly",
                   "every day", "every hour", "periodic", "at midnight")
_ONESHOT_WORDS = ("one-time", "one time", "run once", "batch job", "migration job", "seed job",
                  "init job", "a job that")
_QUEUE_WORDS = ("celery", "sidekiq", "rabbitmq", "kafka", "amqp", "queue worker", "background worker",
                "task worker", "consumer", "worker that")
_DB_WORDS = ("postgres", "postgresql", "mysql", "mariadb", "mongo", "mongodb", "cassandra",
             "cockroach", "database", "redis", "memcached", "etcd")
_ML_WORDS = ("machine learning", "ml model", "model serving", "model server", "inference",
             "pytorch", "tensorflow", "scikit", "sklearn", "huggingface", "llm serving")
_STATIC_WORDS = ("static site", "static website", "static files", "html site", "landing page")
_API_WORDS = ("api", "rest ", "restful", "grpc", "graphql", "fastapi", "flask", "express",
              "backend service", "microservice")
_WEB_WORDS = ("web app", "web application", "webapp", "web server", "website", "web site",
              "frontend", "front-end", "nginx", "http server", "dashboard")

# Ports a Dockerfile or compose file EXPOSEs, and ports named in plain English.
_EXPOSE_RE = re.compile(r"^\s*EXPOSE\s+(\d{1,5})", re.IGNORECASE | re.MULTILINE)
_COMPOSE_PORT_RE = re.compile(r"(\d{1,5})\s*:\s*(\d{1,5})")
_PROMPT_PORT_RE = re.compile(r"\bport\s+(\d{1,5})\b", re.IGNORECASE)

# Names that look like a secret the app will need.
_SECRET_RE = re.compile(r"\b([A-Z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN|API[_-]?KEY|PRIVATE[_-]?KEY)[A-Z0-9_]*)\b")

# Default container ports by type, used only when nothing in the request names one.
_DEFAULT_PORT = {
    WorkloadType.web_app: 8080, WorkloadType.api: 8080, WorkloadType.ml_service: 8080,
    WorkloadType.static_site: 8080,
}

# Which types are reachable from outside by default, and which keep data on disk.
_PUBLIC_BY_DEFAULT = {WorkloadType.web_app, WorkloadType.api, WorkloadType.static_site}
_STORAGE_BY_DEFAULT = {WorkloadType.database}


def _classify_type(text: str) -> Optional[WorkloadType]:
    """The workload type the words in `text` point to, or None if nothing is clear."""
    t = f" {text.lower()} "
    if any(w in t for w in _SCHEDULE_WORDS):
        return WorkloadType.cron_job
    if any(w in t for w in _ONESHOT_WORDS):
        return WorkloadType.one_time_job
    if any(w in t for w in _QUEUE_WORDS):
        return WorkloadType.queue_worker
    if any(w in t for w in _DB_WORDS):
        return WorkloadType.database
    if any(w in t for w in _ML_WORDS):
        return WorkloadType.ml_service
    if any(w in t for w in _STATIC_WORDS):
        return WorkloadType.static_site
    if any(w in t for w in _API_WORDS):
        return WorkloadType.api
    if any(w in t for w in _WEB_WORDS):
        return WorkloadType.web_app
    return None


def _scan_ports(prompt: str, files: Dict[str, str]) -> Optional[int]:
    """The first container port the request or a pasted file names, if any."""
    for content in files.values():
        if not isinstance(content, str):
            continue
        expose = _EXPOSE_RE.search(content)
        if expose:
            return int(expose.group(1))
    compose = _COMPOSE_PORT_RE.search(" ".join(c for c in files.values() if isinstance(c, str)))
    if compose:
        return int(compose.group(2))        # the container side of host:container
    named = _PROMPT_PORT_RE.search(prompt)
    if named:
        return int(named.group(1))
    return None


def deterministic_scan(prompt: str, files: Optional[Dict[str, str]] = None) -> Dict:
    """Facts the scan is confident about. Only keys with real evidence are returned, so
    an empty-ish request yields an almost-empty dict (everything else becomes an unknown)."""
    files = files or {}
    blob = prompt + "\n" + "\n".join(c for c in files.values() if isinstance(c, str))
    facts: Dict = {}

    wt = _classify_type(blob)
    if wt is not None:
        facts["workload_type"] = wt

    port = _scan_ports(prompt, files)
    if port is not None:
        facts["port"] = port

    # Storage: a datastore, or an explicit mention of keeping data.
    if wt in _STORAGE_BY_DEFAULT or re.search(r"\b(persist|persistent|volume|storage|stateful|data on disk)\b", blob, re.IGNORECASE):
        facts["needs_storage"] = True

    # Public access: an explicit mention, or a type that is public by default.
    if re.search(r"\b(public|internet|external|ingress|load ?balancer|expose|publicly)\b", blob, re.IGNORECASE):
        facts["needs_public_access"] = True
    elif wt in _PUBLIC_BY_DEFAULT:
        facts["needs_public_access"] = True

    secrets = sorted({m.group(1) for m in _SECRET_RE.finditer(blob)})
    if secrets:
        facts["secrets_needed"] = secrets

    return facts


def _apply_defaults(profile: ProjectProfile) -> ProjectProfile:
    """Fill the gaps with safe defaults and record each one in `unknowns`, so the plan can show them."""
    unknowns: List[str] = list(profile.unknowns)

    if not profile.image:
        unknowns.append("image: none provided — a specific image:tag must be set before deploy")

    if profile.port is None and profile.workload_type in _DEFAULT_PORT:
        profile.port = _DEFAULT_PORT[profile.workload_type]
        unknowns.append(f"port: not stated — defaulted to {profile.port}")

    # De-duplicate while keeping order.
    seen = set()
    profile.unknowns = [u for u in unknowns if not (u in seen or seen.add(u))]
    return profile


def reconcile(llm_profile: ProjectProfile, facts: Dict) -> ProjectProfile:
    """Merge the LLM's draft with the scan. The scan wins every field it is confident about."""
    data = llm_profile.model_dump()
    conflicts: List[str] = []
    for key, value in facts.items():
        if key in data and data[key] not in (None, "", [], False) and data[key] != value:
            conflicts.append(f"{key}: used scan value {value!r} over model value {data[key]!r}")
        data[key] = value
    merged = ProjectProfile.model_validate(data)
    merged.unknowns = list(merged.unknowns) + conflicts
    return merged


def build_profile(prompt: str, files: Optional[Dict[str, str]], llm=None,
                  role: str = "developer") -> ProjectProfile:
    """The whole profiler: scan, ask the LLM (if given), let the scan win, then fill unknowns.

    If the LLM is absent or cannot produce a valid profile, a scan-only profile is used —
    profiling is grounding, so it never crashes the run; the Gauntlet and human still gate.
    """
    facts = deterministic_scan(prompt, files)

    llm_profile: Optional[ProjectProfile] = None
    if llm is not None and hasattr(llm, "profile"):
        try:
            llm_profile = llm.profile(prompt, files or {}, facts, role=role)
        except Exception:
            llm_profile = None

    if llm_profile is None:
        llm_profile = _scan_only_profile(prompt, facts)

    return _apply_defaults(reconcile(llm_profile, facts))


def _scan_only_profile(prompt: str, facts: Dict) -> ProjectProfile:
    """A ProjectProfile built from the scan alone (fallback when the LLM is unavailable)."""
    wt = facts.get("workload_type", WorkloadType.web_app)
    return ProjectProfile(
        workload_type=wt,
        app_name=_guess_name(prompt),
        port=facts.get("port"),
        needs_storage=facts.get("needs_storage", wt in _STORAGE_BY_DEFAULT),
        needs_public_access=facts.get("needs_public_access", wt in _PUBLIC_BY_DEFAULT),
        secrets_needed=facts.get("secrets_needed", []),
        unknowns=["workload_type: inferred by the deterministic scan (no model)"]
        if "workload_type" in facts else ["workload_type: defaulted to web_app (could not tell)"],
    )


def _guess_name(prompt: str) -> str:
    """A short DNS-safe name from a 'called X' / 'named X' phrase, else 'app'."""
    m = re.search(r"\b(?:called|named)\s+([a-zA-Z][a-zA-Z0-9-]{0,40})", prompt)
    candidate = m.group(1).lower() if m else "app"
    candidate = re.sub(r"[^a-z0-9-]", "-", candidate).strip("-")
    return candidate or "app"
