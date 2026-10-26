"""Deterministic input guardrails. They run before any model call, so a blocked
request spends zero tokens and never reaches the cluster.

Four checks, each with a stable reason code that is logged and surfaced:

  off_topic         - the request is not about Kubernetes / infrastructure
  injection_attempt - prompt injection, jailbreak, or a request to touch the cluster directly
  unauthorized      - the role may not submit, or may not target the requested environment
  rate_limited      - the user exceeded the per-minute or per-day request budget

`check_guardrails(state) -> str` is the stateless entry point used by the graph, the
benchmark and the evaluator: it returns "" when the request is allowed, or "code: reason"
when blocked. It does NOT rate limit (the benchmark calls it to build a dry-run refusal
list). Rate limiting lives in `RateLimiter`, wired into the live graph only.
"""
import logging
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

from contracts import RunState

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- roles

# Canonical access-control roles, with the legacy names mapped onto them. Keeping the
# legacy names as aliases means the Gauntlet's ROLE_PACKS, the rule list and the saved
# run logs all keep working unchanged.
ROLE_ALIASES: Dict[str, str] = {
    "viewer": "viewer",
    "developer": "developer",
    "admin": "admin",
    "junior_dev": "developer",
    "senior_dev": "developer",
    "platform_admin": "admin",
}

ALL_ENVS = ("dev", "staging", "prod")


@dataclass(frozen=True)
class RolePolicy:
    can_submit: bool
    envs: Set[str]


ROLE_POLICY: Dict[str, RolePolicy] = {
    # Viewers are read-only: they may browse runs but never submit a request.
    "viewer": RolePolicy(can_submit=False, envs=set()),
    # Developers own the non-production environments.
    "developer": RolePolicy(can_submit=True, envs={"dev", "staging"}),
    # Only admins may target production.
    "admin": RolePolicy(can_submit=True, envs=set(ALL_ENVS)),
}


def normalize_role(role: Optional[str]) -> str:
    """Maps any accepted role name to one of viewer/developer/admin.

    An unknown role is treated as a viewer (least privilege): it cannot submit.
    """
    return ROLE_ALIASES.get((role or "").strip().lower(), "viewer")


# -------------------------------------------------------------------------- checks

# Words that mark a request as infrastructure-related. Deterministic and broad enough
# that a genuine Kubernetes request is not mistaken for off-topic.
INFRA_KEYWORDS = (
    "server", "database", "db", "cluster", "deploy", "deployment", "kubernetes", "k8s",
    "pod", "service", "svc", "namespace", "ingress", "configmap", "secret", "replica",
    "replicas", "container", "image", "helm", "argocd", "statefulset", "daemonset",
    "cronjob", "job", "pvc", "volume", "node", "scale", "rollout", "manifest", "yaml",
    "nginx", "redis", "postgres", "mysql", "mongodb", "rabbitmq", "kafka", "app",
    "autoscal", "hpa", "network policy", "networkpolicy", "load balancer", "loadbalancer",
    "infra", "infrastructure", "port", "probe", "resource limit",
)

# Prompt-injection, jailbreak and direct-cluster-access patterns. Case-insensitive.
INJECTION_PATTERNS = (
    r"ignore\s+(all\s+|the\s+)?(previous|prior|above|earlier)",
    r"disregard\s+(all\s+|the\s+)?(previous|prior|above|instructions|rules)",
    r"forget\s+(all\s+|everything\s+|your\s+)?(previous|prior|instructions|rules)",
    r"you\s+are\s+now\b",
    r"\bact\s+as\b", r"\bpretend\s+to\s+be\b", r"\bpretend\s+you\b",
    r"system\s+prompt", r"your\s+(system\s+)?(instructions|prompt|rules)",
    r"reveal\s+(your|the)\b", r"show\s+me\s+(your|the)\s+(system|prompt|instructions)",
    r"\bjailbreak\b", r"\bbypass\b", r"override\s+(the\s+)?(guardrail|policy|safety|rules)",
    r"developer\s+mode", r"\bdan\s+mode\b", r"without\s+(any\s+)?restrictions",
    # Attempts to make the system touch the cluster directly.
    r"\bkubectl\b", r"\bkubeconfig\b", r"\bhelm\s+install\b",
    r"(run|execute|apply)\s+(the\s+)?(following|this|these)\b",
    r"delete\s+(all\s+)?(namespace|pod|deployment|everything)",
    r"--all\b.*--force|--force\b.*--all",
    r"\brm\s+-rf\b", r"\bdrop\s+table\b",
    r"\bcurl\b", r"\bwget\b", r"base64\s+(-d|--decode)",
)
_INJECTION_RE = re.compile("|".join(INJECTION_PATTERNS), re.IGNORECASE)
# A long run of base64-ish characters usually hides an encoded payload.
_ENCODED_RE = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")
_HEX_RE = re.compile(r"(?:\\x[0-9a-fA-F]{2}){6,}")


def is_on_topic(prompt: str) -> bool:
    """True if the request mentions anything infrastructure-related."""
    text = prompt.lower()
    return any(kw in text for kw in INFRA_KEYWORDS)


def injection_reason(prompt: str) -> Optional[str]:
    """A short reason if the prompt looks like injection / direct cluster access, else None."""
    match = _INJECTION_RE.search(prompt)
    if match:
        return f"matched a disallowed pattern: '{match.group(0).strip()}'"
    if _ENCODED_RE.search(prompt):
        return "contains a long encoded (base64-like) payload"
    if _HEX_RE.search(prompt):
        return "contains an encoded (hex-escaped) payload"
    return None


def detect_environment(prompt: str) -> str:
    """The target environment named in the prompt: 'prod', 'staging' or 'dev' (the default)."""
    text = prompt.lower()
    if re.search(r"\bprod(uction)?\b", text):
        return "prod"
    if re.search(r"\bstag(e|ing)\b", text):
        return "staging"
    return "dev"


# -------------------------------------------------------------------------- result

@dataclass
class GuardrailResult:
    """The outcome of the deterministic checks for one request."""
    allowed: bool
    code: str = ""
    message: str = ""
    role: str = ""          # canonical role
    environment: str = ""   # detected target environment

    def as_reason(self) -> str:
        """"" when allowed, else "code: message" (the string the graph stores)."""
        return "" if self.allowed else f"{self.code}: {self.message}"


def evaluate(state: RunState) -> GuardrailResult:
    """Runs scope, injection and role checks (no rate limit). The single source of truth
    for the reason codes; `check_guardrails` just returns its string form."""
    prompt = state.user_prompt or ""
    role = normalize_role(state.user_role)
    env = detect_environment(prompt)

    # 1. Injection first: it is the most serious and can hide in an otherwise on-topic prompt.
    reason = injection_reason(prompt)
    if reason:
        return GuardrailResult(False, "injection_attempt", f"possible prompt injection: {reason}", role, env)

    # 2. Scope.
    if not is_on_topic(prompt):
        return GuardrailResult(
            False, "off_topic", "request does not appear to be about Kubernetes/infrastructure", role, env
        )

    # 3. Role enforcement.
    policy = ROLE_POLICY[role]
    if not policy.can_submit:
        return GuardrailResult(
            False, "unauthorized", f"role '{state.user_role}' (viewer) may not submit requests", role, env
        )
    if env not in policy.envs:
        return GuardrailResult(
            False, "unauthorized",
            f"role '{state.user_role}' may not target the '{env}' environment", role, env,
        )

    return GuardrailResult(True, role=role, environment=env)


def check_guardrails(state: RunState) -> str:
    """Stateless guardrail check. "" if allowed, else "code: reason". Backward-compatible API."""
    return evaluate(state).as_reason()


# ---------------------------------------------------------------------- rate limit

class RateLimiter:
    """In-memory per-user request budget: a cap per minute and per day.

    Stateful, so it is kept out of `check_guardrails`; the graph owns one instance.
    `clock` is injectable for tests.
    """

    def __init__(self, per_minute: int = 30, per_day: int = 500, clock=time.time):
        self.per_minute = per_minute
        self.per_day = per_day
        self.clock = clock
        self._events: Dict[str, List[float]] = {}

    def check(self, user_id: str) -> Optional[str]:
        """Records a request and returns a reason string if it should be blocked, else None."""
        now = self.clock()
        user_id = user_id or "anonymous"
        events = [t for t in self._events.get(user_id, []) if now - t < 86400]
        if sum(1 for t in events if now - t < 60) >= self.per_minute:
            self._events[user_id] = events
            return f"more than {self.per_minute} requests in one minute"
        if len(events) >= self.per_day:
            self._events[user_id] = events
            return f"more than {self.per_day} requests in one day"
        events.append(now)
        self._events[user_id] = events
        return None

    def reset(self) -> None:
        self._events.clear()
