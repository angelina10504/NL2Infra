"""Project-Aware Generation: the 'fit' layer.

Loads the per-type rules from requirements/<workload_type>.yaml (the kinds and the
must-have list live there as data, never hardcoded here) and provides:

  - expected_kinds / must_haves : what the plan should create and contain,
  - fit_conformance            : deterministic checks that every must-have actually
                                 appears in the generated YAML (feeds the fix loop),
  - examples_for               : the small correct examples used to ground generation.

The detection primitives below are code; the lists of kinds and must-haves are data.
"""
import functools
import os
from typing import Dict, List, Optional

import yaml

from contracts import Plan, ProjectProfile, Violation, WORKLOAD_KINDS, WorkloadType

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
REQUIREMENTS_DIR = os.path.join(_ROOT, "requirements")
EXAMPLES_DIR = os.path.join(_ROOT, "examples")


def _type_name(workload_type) -> str:
    return workload_type.value if isinstance(workload_type, WorkloadType) else str(workload_type)


@functools.lru_cache(maxsize=None)
def load_requirements(workload_type) -> dict:
    """The requirements/<type>.yaml for this workload, as a dict. Cached per process."""
    name = _type_name(workload_type)
    path = os.path.join(REQUIREMENTS_DIR, f"{name}.yaml")
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    data.setdefault("kinds", [])
    data.setdefault("must_have", [])
    return data


def expected_kinds(workload_type) -> List[str]:
    return list(load_requirements(workload_type).get("kinds", []))


def must_haves(workload_type) -> List[dict]:
    return list(load_requirements(workload_type).get("must_have", []))


# --------------------------------------------------------------- must-have detectors
# Each detector takes the parsed resources and returns a list of (resource, detail) failures.

def _workloads(resources):
    return [r for r in resources if r.kind.lower() in WORKLOAD_KINDS]


def _app_containers(res):
    return [c for c in (res.pod_spec().get("containers") or []) if isinstance(c, dict)]


def _missing_resource_limits(resources):
    out = []
    for res in _workloads(resources):
        for c in _app_containers(res):
            limits = ((c.get("resources") or {}).get("limits")) or {}
            if not (limits.get("cpu") and limits.get("memory")):
                out.append((res, f"container '{c.get('name', '?')}' needs resources.limits.cpu and memory"))
    return out


def _missing_probe(resources, probe_key):
    out = []
    for res in _workloads(resources):
        for c in _app_containers(res):
            if not c.get(probe_key):
                out.append((res, f"container '{c.get('name', '?')}' needs a {probe_key}"))
    return out


def _missing_volume_claim(resources):
    out = []
    stateful = [r for r in resources if r.kind.lower() == "statefulset"]
    for res in stateful:
        spec = res.doc.get("spec") if isinstance(res.doc, dict) else {}
        if not (isinstance(spec, dict) and spec.get("volumeClaimTemplates")):
            out.append((res, "a StatefulSet needs spec.volumeClaimTemplates so data persists"))
    if not stateful:
        # No StatefulSet at all: the database's storage cannot exist.
        return [(None, "no StatefulSet with volumeClaimTemplates was generated")]
    return out


def _missing_schedule(resources):
    out = []
    crons = [r for r in resources if r.kind.lower() == "cronjob"]
    for res in crons:
        spec = res.doc.get("spec") if isinstance(res.doc, dict) else {}
        if not (isinstance(spec, dict) and spec.get("schedule")):
            out.append((res, "a CronJob needs a spec.schedule cron expression"))
    if not crons:
        return [(None, "no CronJob with a schedule was generated")]
    return out


def _missing_secret_ref(resources):
    # At least one container must pull a value from a Secret (envFrom.secretRef or
    # env[].valueFrom.secretKeyRef) when the profile says secrets are needed.
    for res in _workloads(resources):
        for c in _app_containers(res):
            for source in (c.get("envFrom") or []):
                if isinstance(source, dict) and source.get("secretRef"):
                    return []
            for env in (c.get("env") or []):
                if isinstance(env, dict) and isinstance(env.get("valueFrom"), dict) and env["valueFrom"].get("secretKeyRef"):
                    return []
    return [(None, "no container references a Secret, but the request needs one")]


_DETECTORS = {
    "resource_limits": _missing_resource_limits,
    "readiness_probe": lambda r: _missing_probe(r, "readinessProbe"),
    "liveness_probe": lambda r: _missing_probe(r, "livenessProbe"),
    "volume_claim": _missing_volume_claim,
    "schedule": _missing_schedule,
    "secret_ref": _missing_secret_ref,
}


def required_must_haves(profile: ProjectProfile) -> List[dict]:
    """The static must-haves for the type, plus a secret reference when the profile needs one."""
    items = must_haves(profile.workload_type)
    if profile.secrets_needed and not any(m.get("id") == "secret_ref" for m in items):
        items = items + [{"id": "secret_ref", "description": "A reference to a Secret for the credentials the app needs."}]
    return items


def fit_conformance(files: Dict[str, str], profile: Optional[ProjectProfile]) -> List[Violation]:
    """Deterministic 'fit' checks: every must-have for the project type must appear in the YAML.

    Emits tool='plan' violations (so they flow through the existing fix loop, never a crash),
    with clear, actionable messages. Returns [] when there is no profile (feature off).
    """
    if profile is None:
        return []
    from validators import parse_files  # deferred: validators imports fit lazily, breaking the cycle

    resources, _, _ = parse_files(files)
    violations: List[Violation] = []
    type_name = _type_name(profile.workload_type)

    for item in required_must_haves(profile):
        detector = _DETECTORS.get(item.get("id"))
        if detector is None:
            continue
        for res, detail in detector(resources):
            violations.append(Violation(
                tool="plan",
                rule_id=f"FIT_MUSTHAVE_{item['id'].upper()}",
                severity="HIGH",
                file=res.file if res is not None else None,
                line=res.line if res is not None else None,
                resource=res.label if res is not None else None,
                message=f"{type_name} requires {item.get('description', item['id'])} — {detail}.",
            ))
    return violations


# ------------------------------------------------------------------------- examples

@functools.lru_cache(maxsize=None)
def examples_for(workload_type) -> str:
    """The small correct example manifests for this type, concatenated, for prompt grounding.

    A plain by-directory lookup (examples/<type>/*.yaml) — nothing heavy or paid.
    """
    name = _type_name(workload_type)
    folder = os.path.join(EXAMPLES_DIR, name)
    if not os.path.isdir(folder):
        return ""
    blocks = []
    for fname in sorted(os.listdir(folder)):
        if fname.endswith((".yaml", ".yml")):
            with open(os.path.join(folder, fname), encoding="utf-8") as f:
                blocks.append(f"# example: {fname}\n{f.read().strip()}")
    return "\n---\n".join(blocks)


def grounding_block(profile: Optional[ProjectProfile]) -> str:
    """The Project-Aware grounding added to the generate prompt: the type, its must-haves,
    and a correct example or two. Empty when there is no profile."""
    if profile is None:
        return ""
    type_name = _type_name(profile.workload_type)
    lines = [
        f"Project-Aware Generation — this is a '{type_name}'.",
        f"Expected Kubernetes kinds: {', '.join(expected_kinds(profile.workload_type)) or 'n/a'}.",
        "It must contain:",
    ]
    lines += [f"  - {m['description']}" for m in required_must_haves(profile)]
    example = examples_for(profile.workload_type)
    if example:
        lines.append("A correct example of this kind of project (follow its shape, not its exact names):")
        lines.append(example[:1500])
    return "\n".join(lines)
