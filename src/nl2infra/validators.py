import os
import re
import sys
import json
import time
import shutil
import tempfile
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import yaml

from contracts import Plan, Violation, CLUSTER_SCOPED_KINDS, WORKLOAD_KINDS

TOOL_CRASH = "TOOL_CRASH"

# One policy pack per role. A role without an entry has no pack, which is a
# TOOL_CRASH rather than an unchecked run.
ROLE_PACKS = {
    "junior_dev": ["base"],
    "senior_dev": ["base", "staging"],
    "platform_admin": ["base", "staging", "production"],
}

# kubectl failures that say nothing about the manifest: the cluster or the
# credentials are the problem, so the model must not be asked to fix them.
_KUBECTL_ENV_ERRORS = (
    "unable to connect to the server", "connection refused", "dial tcp", "i/o timeout",
    "context deadline exceeded", "failed to download openapi", "tls handshake", "x509:",
    "you must be logged in", "cannot create resource", "cannot patch resource", "cannot get resource",
    "no configuration has been provided", "service unavailable", "the server is currently unable",
    "no route to host", "current-context is not set",
)


def is_tool_crash(violations: List[Violation]) -> bool:
    """True if any checker failed to run. Such a run escalates; it never goes to the fix loop."""
    return any(v.rule_id == TOOL_CRASH for v in violations)


def _crash(tool: str, message: str) -> List[Violation]:
    return [Violation(tool=tool, rule_id=TOOL_CRASH, severity="CRITICAL", message=message)]


def _resolve_binary(name: str) -> Optional[str]:
    """Finds a binary next to the running interpreter, in ~/.local/bin, the project venv, or PATH."""
    candidates = [
        os.path.join(os.path.dirname(sys.executable), name),
        os.path.expanduser(f"~/.local/bin/{name}"),
        os.path.join(os.getcwd(), ".venv", "bin", name),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return shutil.which(name)


def _parse_json_output(raw: str):
    """Parses tool stdout as JSON, tolerating log lines before it. Returns None if there is none."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (raw.find("{"), raw.find("[")) if i != -1]
    if not starts:
        return None
    try:
        return json.loads(raw[min(starts):])
    except json.JSONDecodeError:
        return None


@dataclass
class ParsedResource:
    kind: str
    name: str
    namespace: Optional[str]  # None for cluster-scoped kinds
    file: str
    line: int
    doc: dict

    @property
    def key(self) -> Tuple[str, str]:
        return (self.kind.lower(), self.name.lower())

    @property
    def label(self) -> str:
        return f"{self.kind}/{self.name}"

    def pod_spec(self) -> dict:
        spec = self.doc.get("spec")
        spec = spec if isinstance(spec, dict) else {}
        kind = self.kind.lower()
        if kind == "pod":
            path = []
        elif kind == "cronjob":
            path = ["jobTemplate", "spec", "template", "spec"]
        else:
            path = ["template", "spec"]
        for step in path:
            spec = spec.get(step) if isinstance(spec, dict) else None
        return spec if isinstance(spec, dict) else {}

    def images(self) -> List[str]:
        images = []
        pod_spec = self.pod_spec()
        for key in ("containers", "initContainers"):
            for container in pod_spec.get(key) or []:
                if isinstance(container, dict) and container.get("image") is not None:
                    images.append(str(container["image"]).strip())
        return images

    def replicas(self):
        spec = self.doc.get("spec")
        return spec.get("replicas", 1) if isinstance(spec, dict) else 1


def _find_line(content: str, start_line: int, pattern: str) -> int:
    """First line at or after start_line that matches pattern (1-based); start_line if none does."""
    for number, text in enumerate(content.splitlines(), start=1):
        if number >= start_line and re.match(pattern, text):
            return number
    return start_line


def parse_files(files: Dict[str, str]) -> Tuple[List[ParsedResource], List[Violation], List[str]]:
    """Parses every file. Returns the resources, the parse violations, and the files safe to scan.

    Checkov silently drops a file it cannot parse and Conftest aborts on it, so files
    are checked here first and only well-formed ones are handed to the tools.
    """
    resources: List[ParsedResource] = []
    violations: List[Violation] = []
    scannable: List[str] = []

    for filename, content in files.items():
        if not isinstance(content, str):
            violations.append(Violation(
                tool="plan", rule_id="YAML_SYNTAX_ERROR", severity="CRITICAL", file=filename,
                message=f"Content of {filename} is not text."
            ))
            continue
        found: List[ParsedResource] = []
        bad = False
        try:
            loader = yaml.SafeLoader(content)
            try:
                while loader.check_node():
                    node = loader.get_node()
                    line = node.start_mark.line + 1
                    doc = loader.construct_document(node)
                    if doc is None:
                        continue
                    meta = doc.get("metadata") if isinstance(doc, dict) else None
                    kind = doc.get("kind") if isinstance(doc, dict) else None
                    name = meta.get("name") if isinstance(meta, dict) else None
                    if not isinstance(kind, str) or not kind.strip() or not isinstance(name, str) or not name.strip():
                        bad = True
                        violations.append(Violation(
                            tool="plan", rule_id="PLAN_CONFORMANCE_INVALID_DOC", severity="HIGH",
                            file=filename, line=line,
                            message=f"A document in {filename} is not a Kubernetes object with kind and metadata.name."
                        ))
                        continue
                    kind = kind.strip()
                    namespace = None
                    if kind.lower() not in CLUSTER_SCOPED_KINDS:
                        namespace = str(meta.get("namespace") or "default").strip()
                    found.append(ParsedResource(kind, name.strip(), namespace, filename, line, doc))
            finally:
                loader.dispose()
        except yaml.YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            violations.append(Violation(
                tool="plan", rule_id="YAML_SYNTAX_ERROR", severity="CRITICAL", file=filename,
                line=mark.line + 1 if mark else None,
                message=f"YAML syntax error in {filename}: {' '.join(str(e).split())[:300]}"
            ))
            continue
        resources.extend(found)
        if found and not bad:
            scannable.append(filename)

    return resources, violations, scannable


class Validators:
    def __init__(self, policies_dir: Optional[str] = None):
        self.policies_dir = policies_dir or os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "policies")
        )
        self.checkov_config = os.path.join(self.policies_dir, "checkov.yaml")
        self.checkov_bin = _resolve_binary("checkov")
        self.conftest_bin = _resolve_binary("conftest")
        self.kubectl_bin = _resolve_binary("kubectl")
        self.last_timings: Dict[str, float] = {}  # seconds per tool in the latest validate()
        self.last_scan: Dict[str, int] = {}       # files given, files scanned and resources found, latest validate()
        self._tool_versions: Optional[Dict[str, str]] = None

    def skipped_checks(self) -> List[str]:
        """Checkov rules skipped on purpose (policies/checkov.yaml); recorded in every run log."""
        try:
            with open(self.checkov_config, encoding="utf-8") as f:
                return list((yaml.safe_load(f) or {}).get("skip-check") or [])
        except OSError:
            return []

    def tool_versions(self) -> Dict[str, str]:
        """Versions of the three external tools, read once per process. 'unavailable' if a tool cannot answer."""
        if self._tool_versions is None:
            def ask(binary, args, pattern):
                if not binary:
                    return "unavailable"
                try:
                    proc = subprocess.run([binary, *args], capture_output=True, text=True, timeout=30)
                    found = re.search(pattern, proc.stdout or "")
                    return found.group(1) if proc.returncode == 0 and found else "unavailable"
                except Exception:
                    return "unavailable"

            self._tool_versions = {
                "checkov": ask(self.checkov_bin, ["--version"], r"(\d[\w.\-]*)"),
                "conftest": ask(self.conftest_bin, ["--version"], r"Conftest:\s*(\S+)"),
                "opa": ask(self.conftest_bin, ["--version"], r"OPA:\s*(\S+)"),
                "kubectl": ask(self.kubectl_bin, ["version", "--client", "-o", "json"], r'"gitVersion":\s*"([^"]+)"'),
                "kubernetes": ask(self.kubectl_bin, ["get", "--raw", "/version", "--request-timeout=10s"],
                                  r'"gitVersion":\s*"([^"]+)"'),
            }
        return dict(self._tool_versions)

    # ------------------------------------------------------------------ plan

    def _plan_conformance(self, files: Dict[str, str], plan: Plan) -> List[Violation]:
        """The files must contain exactly the approved plan.

        Every planned resource exists with the same kind, name, namespace, image
        and replica count, and nothing else is present.
        """
        resources, violations, _ = parse_files(files)
        unused = list(resources)
        planned_keys = set()

        for res in plan.resources:
            label = f"{res.type}/{res.name}"
            key = (res.type.lower(), res.name.lower())
            planned_keys.add(key)
            cluster_scoped = key[0] in CLUSTER_SCOPED_KINDS
            target_ns = None if cluster_scoped else (res.namespace or "default")

            candidates = [r for r in unused if r.key == key]
            if not candidates:
                violations.append(Violation(
                    tool="plan", rule_id="PLAN_CONFORMANCE_MISSING", severity="HIGH", resource=label,
                    message=f"Planned resource {label} (namespace: {res.namespace or '-'}) is missing from the generated files."
                ))
                continue
            found = next((r for r in candidates if r.namespace == target_ns), candidates[0])
            unused.remove(found)
            content = files[found.file]

            if found.namespace != target_ns:
                violations.append(Violation(
                    tool="plan", rule_id="PLAN_CONFORMANCE_NAMESPACE", severity="HIGH",
                    file=found.file, line=_find_line(content, found.line, r"\s*namespace\s*:"), resource=label,
                    message=f"{label} namespace mismatch: the plan says '{target_ns}', the file says '{found.namespace}'."
                ))

            expected_images = sorted(res.planned_images())
            if key[0] in WORKLOAD_KINDS and expected_images != sorted(found.images()):
                violations.append(Violation(
                    tool="plan", rule_id="PLAN_CONFORMANCE_IMAGE", severity="HIGH",
                    file=found.file, line=_find_line(content, found.line, r"\s*(-\s*)?image\s*:"), resource=label,
                    message=(
                        f"{label} image mismatch: the plan says {expected_images}, the file says {sorted(found.images())}. "
                        "Use exactly the planned image string. Do not change the tag, add a digest or add containers."
                    )
                ))

            expected_replicas = res.planned_replicas()
            if expected_replicas is not None and found.replicas() != expected_replicas:
                violations.append(Violation(
                    tool="plan", rule_id="PLAN_CONFORMANCE_REPLICAS", severity="HIGH",
                    file=found.file, line=_find_line(content, found.line, r"\s*replicas\s*:"), resource=label,
                    message=f"{label} replica mismatch: the plan says {expected_replicas}, the file says {found.replicas()}."
                ))

        for extra in unused:
            duplicate = extra.key in planned_keys
            violations.append(Violation(
                tool="plan",
                rule_id="PLAN_CONFORMANCE_DUPLICATE" if duplicate else "PLAN_CONFORMANCE_UNPLANNED",
                severity="HIGH", file=extra.file, line=extra.line, resource=extra.label,
                message=(
                    f"{extra.label} is declared more than once; keep one definition."
                    if duplicate else
                    f"{extra.label} in {extra.file} is not in the approved plan. Remove it; do not add resources."
                )
            ))

        return violations

    # --------------------------------------------------------------- checkov

    def _run_checkov(self, temp_dir: str, names: Optional[Dict[str, str]] = None,
                     expect_resources: bool = False) -> List[Violation]:
        """Runs Checkov. Anything other than well-formed results is a TOOL_CRASH."""
        names = names or {}
        bin_path = self.checkov_bin or _resolve_binary("checkov")
        if not bin_path:
            return _crash("checkov", "Checkov binary not found in PATH or virtual environment.")
        if not os.path.isfile(self.checkov_config):
            return _crash("checkov", f"Checkov config not found: {self.checkov_config}")

        cmd = [
            bin_path, "-d", temp_dir, "--framework", "kubernetes", "--output", "json",
            "--soft-fail", "--quiet", "--compact", "--skip-download",
            "--config-file", self.checkov_config,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return _crash("checkov", "Checkov scan timed out after 120 seconds.")
        except Exception as e:
            return _crash("checkov", f"Checkov execution failed: {e}")

        if proc.returncode != 0:
            err_msg = (proc.stderr or "").strip() or (proc.stdout or "").strip() or "no output"
            return _crash("checkov", f"Checkov crashed with exit code {proc.returncode}: {err_msg[:300]}")

        parsed = _parse_json_output(proc.stdout or "")
        if parsed is None:
            return _crash("checkov", f"Checkov returned no parseable JSON: {(proc.stdout or '').strip()[:200]!r}")

        violations: List[Violation] = []
        for item in parsed if isinstance(parsed, list) else [parsed]:
            if not isinstance(item, dict):
                return _crash("checkov", "Checkov JSON has an unexpected shape (item is not an object).")
            summary = item.get("summary") if "results" in item else item
            if not isinstance(summary, dict) or "checkov_version" not in summary or "failed" not in summary:
                return _crash("checkov", "Checkov JSON has no summary; cannot tell a pass from a failure.")
            if summary.get("parsing_errors"):
                return _crash("checkov", f"Checkov could not parse {summary['parsing_errors']} file(s).")

            if "results" not in item:
                # Summary only: Checkov found no resource it has checks for (e.g. only a Namespace).
                if summary.get("failed") or expect_resources:
                    return _crash("checkov", "Checkov reported no results for files that contain workloads.")
                continue

            failed_checks = item["results"].get("failed_checks") if isinstance(item["results"], dict) else None
            if not isinstance(failed_checks, list) or len(failed_checks) != summary.get("failed"):
                return _crash("checkov", "Checkov results do not match its summary.")

            for check in failed_checks:
                if not isinstance(check, dict) or not check.get("check_id"):
                    return _crash("checkov", "Checkov returned a failed check without an id.")
                tmp_name = os.path.basename(check.get("file_path") or "")
                line_range = check.get("file_line_range")
                severity = str(check.get("severity") or "MEDIUM").upper()
                violations.append(Violation(
                    tool="checkov",
                    rule_id=check["check_id"],
                    # Open-source Checkov reports no severity; MEDIUM is our default.
                    severity=severity if severity in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO") else "MEDIUM",
                    file=names.get(tmp_name, tmp_name) or None,
                    line=line_range[0] if isinstance(line_range, list) and line_range else None,
                    message=check.get("check_name") or "Checkov policy failed",
                    resource=check.get("resource"),
                ))
        return violations

    # -------------------------------------------------------------- conftest

    def _policy_paths(self, role: str) -> Tuple[List[str], Optional[str]]:
        packs = ROLE_PACKS.get(role)
        if not packs:
            return [], f"No policy pack is defined for role '{role}'."
        paths = []
        for pack in packs:
            path = os.path.join(self.policies_dir, pack)
            has_rules = os.path.isdir(path) and any(
                f.endswith(".rego") and not f.endswith("_test.rego") for f in os.listdir(path)
            )
            if not has_rules:
                return [], f"Policy pack '{pack}' is missing or empty: {path}"
            paths.append(path)
        return paths, None

    def _run_conftest(self, temp_dir: str, role: str, names: Optional[Dict[str, str]] = None,
                      lines: Optional[Dict[Tuple[str, str], int]] = None) -> List[Violation]:
        """Runs Conftest with the role's policy packs. Anything but well-formed results is a TOOL_CRASH."""
        names = names or {}
        lines = lines or {}
        bin_path = self.conftest_bin or _resolve_binary("conftest")
        if not bin_path:
            return _crash("opa", "Conftest binary not found in PATH or ~/.local/bin.")

        policy_paths, error = self._policy_paths(role)
        if error:
            return _crash("opa", error)

        cmd = [bin_path, "test"]
        for p in policy_paths:
            cmd.extend(["--policy", p])
        cmd.extend(["--output", "json", "--no-color", temp_dir])

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            return _crash("opa", "Conftest timed out after 60 seconds.")
        except Exception as e:
            return _crash("opa", f"Conftest execution failed: {e}")

        parsed = _parse_json_output(proc.stdout or "")
        # Exit 0 = no failures, 1 = failures (or an error, which prints no JSON).
        if proc.returncode not in (0, 1) or not isinstance(parsed, list):
            err_msg = (proc.stderr or "").strip() or (proc.stdout or "").strip() or "no output"
            return _crash("opa", f"Conftest crashed with exit code {proc.returncode}: {err_msg[:300]}")

        violations: List[Violation] = []
        evaluated = set()
        for file_res in parsed:
            if not isinstance(file_res, dict) or not file_res.get("filename"):
                return _crash("opa", "Conftest JSON has an unexpected shape.")
            tmp_name = os.path.basename(file_res["filename"])
            filename = names.get(tmp_name, tmp_name)
            failures = file_res.get("failures") or []
            warnings = file_res.get("warnings") or []
            if not isinstance(file_res.get("successes"), int) or \
                    file_res["successes"] + len(failures) + len(warnings) + len(file_res.get("exceptions") or []) == 0:
                return _crash("opa", f"Conftest evaluated no rules for {filename}.")
            evaluated.add(tmp_name)

            for entries, severity, default_id in ((failures, "HIGH", "OPA_RULE_FAILED"), (warnings, "MEDIUM", "OPA_WARNING")):
                for entry in entries:
                    msg = entry.get("msg", "") if isinstance(entry, dict) else str(entry)
                    meta = entry.get("metadata") if isinstance(entry, dict) else None
                    meta = meta if isinstance(meta, dict) else {}
                    rule_id = meta.get("rule_id")
                    tagged = re.match(r"^\[([A-Z0-9_-]+)\]\s*(.*)", msg, re.S)
                    if tagged:
                        rule_id = rule_id or tagged.group(1)
                        msg = tagged.group(2)
                    resource = meta.get("resource")
                    violations.append(Violation(
                        tool="opa",
                        rule_id=rule_id or default_id,
                        severity=severity,
                        file=filename,
                        line=lines.get((filename, resource)),
                        message=msg or "OPA policy violation",
                        resource=resource,
                    ))

        expected = {f for f in os.listdir(temp_dir) if f.endswith((".yaml", ".yml"))}
        if expected - evaluated:
            return _crash("opa", f"Conftest did not evaluate: {', '.join(sorted(names.get(f, f) for f in expected - evaluated))}")
        if proc.returncode == 1 and not any(v.severity == "HIGH" for v in violations):
            return _crash("opa", f"Conftest exited 1 without reporting a failure: {(proc.stderr or '').strip()[:300]}")
        return violations

    # --------------------------------------------------------------- kubectl

    def _kubectl(self, args: List[str]) -> subprocess.CompletedProcess:
        return subprocess.run([self.kubectl_bin, *args], capture_output=True, text=True, timeout=45)

    def _dry_run_in_existing_namespace(self, path: str, namespace: str) -> subprocess.CompletedProcess:
        """Dry-runs a copy of the file with `namespace` swapped for 'default'.

        A server dry-run persists nothing, so a resource in a namespace that the same
        file set creates is rejected with "namespace not found". Its schema is checked
        against an existing namespace instead; the files themselves are not changed.
        """
        with open(path, encoding="utf-8") as f:
            docs = [d for d in yaml.safe_load_all(f) if isinstance(d, dict)]
        docs = [d for d in docs if not (d.get("kind") == "Namespace" and d.get("metadata", {}).get("name") == namespace)]
        for doc in docs:
            if isinstance(doc.get("metadata"), dict) and doc["metadata"].get("namespace") == namespace:
                doc["metadata"]["namespace"] = "default"
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as copy:
            yaml.safe_dump_all(docs, copy)
        try:
            return self._kubectl(["apply", "--dry-run=server", "--request-timeout=30s", "-f", copy.name])
        finally:
            os.unlink(copy.name)

    def _run_kubectl_dry_run(self, temp_dir: str, names: Optional[Dict[str, str]] = None,
                             planned_namespaces: Optional[set] = None,
                             declared_namespaces: Optional[set] = None) -> List[Violation]:
        """Server-side dry-run of every file. There is no client-side fallback.

        A missing kubectl or an unreachable cluster is a TOOL_CRASH. Only what the
        API server says about the manifest itself becomes a fixable violation.
        """
        names = names or {}
        planned_namespaces = planned_namespaces or set()
        declared_namespaces = declared_namespaces or set()
        self.kubectl_bin = self.kubectl_bin or _resolve_binary("kubectl")
        if not self.kubectl_bin:
            return _crash("dry-run", "kubectl binary not found; server-side dry-run cannot run.")

        try:
            # Reachability first, retried once, so an outage is never blamed on a manifest.
            probe = self._kubectl(["get", "--raw", "/version", "--request-timeout=10s"])
            if probe.returncode != 0:
                probe = self._kubectl(["get", "--raw", "/version", "--request-timeout=10s"])
            if probe.returncode != 0:
                return _crash("dry-run", f"Cluster unreachable for dry-run: {(probe.stderr or '').strip()[:300]}")

            violations: List[Violation] = []
            for tmp_name in sorted(f for f in os.listdir(temp_dir) if f.endswith((".yaml", ".yml"))):
                filename = names.get(tmp_name, tmp_name)
                path = os.path.join(temp_dir, tmp_name)
                proc = self._kubectl(["apply", "--dry-run=server", "--request-timeout=30s", "-f", path])
                created_here = re.search(r'namespaces "([^"]+)" not found', proc.stderr or "")
                if proc.returncode != 0 and created_here and created_here.group(1) in declared_namespaces:
                    proc = self._dry_run_in_existing_namespace(path, created_here.group(1))
                if proc.returncode == 0:
                    if "(server dry run)" not in (proc.stdout or ""):
                        return _crash("dry-run", f"kubectl exited 0 for {filename} without a server dry-run result.")
                    continue

                err_msg = (proc.stderr or "").strip().replace(os.path.join(temp_dir, tmp_name), filename)
                err_msg = err_msg.replace(temp_dir + os.sep, "") or f"kubectl exited {proc.returncode} with no message"
                lowered = err_msg.lower()
                if any(marker in lowered for marker in _KUBECTL_ENV_ERRORS):
                    return _crash("dry-run", f"Cluster error during dry-run of {filename}: {err_msg[:300]}")
                missing_ns = re.search(r'namespaces "([^"]+)" not found', err_msg)
                if missing_ns and missing_ns.group(1) in planned_namespaces:
                    # The plan targets this namespace, so the manifest is right and the
                    # validation cluster is not set up for it.
                    return _crash(
                        "dry-run",
                        f"Namespace '{missing_ns.group(1)}' does not exist in the validation cluster; "
                        "create it before running (make namespaces)."
                    )
                line = re.search(r"\bline (\d+)", err_msg)
                violations.append(Violation(
                    tool="dry-run", rule_id="KUBECTL_DRY_RUN_FAILED", severity="HIGH", file=filename,
                    line=int(line.group(1)) if line else None,
                    message=f"Kubernetes server dry-run rejected {filename}: {err_msg[:400]}"
                ))
            return violations
        except subprocess.TimeoutExpired:
            return _crash("dry-run", "kubectl timed out; the cluster did not answer.")
        except Exception as e:
            return _crash("dry-run", f"kubectl execution error: {e}")

    # ------------------------------------------------------------------- all

    def validate(self, files: Dict[str, str], plan: Plan, role: str = "junior_dev") -> List[Violation]:
        """Runs the full Gauntlet: plan conformance, Checkov, Conftest, server dry-run."""
        timings: Dict[str, float] = {}

        def timed(name, func, *args, **kwargs):
            started = time.time()
            try:
                return func(*args, **kwargs)
            finally:
                timings[name] = round(time.time() - started, 3)

        self.last_timings = timings
        violations = timed("plan", self._plan_conformance, files, plan)
        resources, _, scannable = parse_files(files)
        self.last_scan = {"files": len(files), "scanned": len(scannable), "resources": len(resources)}

        if not scannable:
            # Nothing well-formed to scan. That must never read as a pass.
            if not violations:
                violations.append(Violation(
                    tool="plan", rule_id="PLAN_CONFORMANCE_MISSING", severity="HIGH",
                    message="No Kubernetes resources were generated."
                ))
            return violations

        with tempfile.TemporaryDirectory(prefix="nl2infra_") as tmpdir:
            tmpdir = os.path.realpath(tmpdir)
            names: Dict[str, str] = {}
            for index, fname in enumerate(scannable):
                safe_fname = os.path.basename(fname) or f"file{index}"
                if not safe_fname.endswith((".yaml", ".yml")):
                    safe_fname = f"{safe_fname}.yaml"
                if safe_fname in names:
                    safe_fname = f"{index}_{safe_fname}"
                names[safe_fname] = fname
                with open(os.path.join(tmpdir, safe_fname), "w", encoding="utf-8") as f:
                    f.write(files[fname])

            scanned = [r for r in resources if r.file in scannable]
            lines = {(r.file, r.label): r.line for r in scanned}
            has_workload = any(r.kind.lower() in WORKLOAD_KINDS for r in scanned)
            planned_namespaces = {
                r.namespace for r in plan.resources if r.type.lower() not in CLUSTER_SCOPED_KINDS and r.namespace
            }

            violations.extend(timed("checkov", self._run_checkov, tmpdir, names, expect_resources=has_workload))
            violations.extend(timed("opa", self._run_conftest, tmpdir, role, names, lines))
            declared_namespaces = {r.name for r in scanned if r.kind.lower() == "namespace"}
            violations.extend(timed(
                "dry-run", self._run_kubectl_dry_run, tmpdir, names, planned_namespaces, declared_namespaces
            ))

        return violations
