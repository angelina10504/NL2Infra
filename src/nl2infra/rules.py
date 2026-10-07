"""The rule list given to the model: what the Gauntlet enforces, in plain text.

Used by the pipeline's generate prompt and by benchmark arm B, so both see exactly
the same rules. The only difference between arm B and arm C is then the fix loop.
"""
import os

from validators import ROLE_PACKS

RULE_LIST_FILE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "benchmarks", "rule_list.txt")
)

# Plain-English form of the Rego rules, by policy pack. tests/test_benchmark.py checks
# these ids against policies/ so the list cannot drift from what the Gauntlet enforces.
OPA_RULES = {
    "base": {
        "OPA_NO_ROOT": "Every container must set securityContext.runAsNonRoot: true (or inherit it from the pod).",
        "OPA_NO_PRIVILEGED": "No container may set securityContext.privileged: true.",
        "OPA_NO_PLAINTEXT_SECRET": "No literal value in an env var whose name contains PASSWORD, PASSWD, SECRET, "
                                   "TOKEN, API_KEY, APIKEY or PRIVATE_KEY; keep the value in a Secret.",
        "OPA_REQUIRE_CPU_LIMIT": "Every container must set resources.limits.cpu.",
        "OPA_REQUIRE_MEM_LIMIT": "Every container must set resources.limits.memory.",
    },
    "staging": {
        "OPA_REQUIRE_READINESS_PROBE": "Every container of a Deployment, StatefulSet, DaemonSet or ReplicaSet needs a readinessProbe.",
        "OPA_DISALLOW_LATEST_TAG": "No image may use the :latest tag or omit the tag.",
    },
    "production": {
        "OPA_PRODUCTION_REPLICAS": "A Deployment or StatefulSet in the production namespace needs at least 2 replicas.",
        "OPA_NO_DEFAULT_NAMESPACE": "No resource may be placed in the default namespace.",
        "OPA_READONLY_ROOTFS": "Every container must set securityContext.readOnlyRootFilesystem: true.",
    },
}


def rules_for_role(role: str) -> str:
    """The Checkov rules in benchmarks/rule_list.txt plus the OPA rules of the role's policy packs."""
    with open(RULE_LIST_FILE, encoding="utf-8") as f:
        checkov_rules = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    opa_rules = [f"{rule}: {text}" for pack in ROLE_PACKS[role] for rule, text in OPA_RULES[pack].items()]
    return "\n".join(f"- {line}" for line in checkov_rules + opa_rules)
