from pydantic import BaseModel, Field, field_validator, model_validator
from typing import List, Dict, Optional, Literal

# Kinds that carry a pod template, so the plan must name their image(s).
WORKLOAD_KINDS = {"deployment", "statefulset", "daemonset", "replicaset", "job", "cronjob", "pod"}
# Kinds with spec.replicas. A missing value means the Kubernetes default of 1.
REPLICATED_KINDS = {"deployment", "statefulset", "replicaset"}
# Kinds without a namespace.
CLUSTER_SCOPED_KINDS = {
    "namespace", "clusterrole", "clusterrolebinding", "persistentvolume", "storageclass",
    "customresourcedefinition", "priorityclass", "ingressclass",
}


class Resource(BaseModel):
    type: str = Field(min_length=1, description="The Kubernetes resource type, e.g., Deployment, Service")
    name: str = Field(min_length=1, description="The name of the resource")
    namespace: str = Field(description="The namespace for the resource")
    spec: dict = Field(description="Key specifications, like image, ports, replicas")

    @field_validator("type", "name", "namespace")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _check(self) -> "Resource":
        if not self.type or not self.name:
            raise ValueError("resource type and name must not be empty")
        kind = self.type.lower()
        if kind not in CLUSTER_SCOPED_KINDS and not self.namespace:
            raise ValueError(f"{self.type}/{self.name}: namespace must not be empty")
        if kind in WORKLOAD_KINDS and not self.planned_images():
            raise ValueError(f"{self.type}/{self.name}: spec.image is required for a workload")
        if kind in REPLICATED_KINDS and "replicas" in self.spec:
            replicas = self.spec["replicas"]
            if isinstance(replicas, bool) or not isinstance(replicas, int) or replicas < 0:
                raise ValueError(f"{self.type}/{self.name}: spec.replicas must be a non-negative integer")
        return self

    def planned_images(self) -> List[str]:
        """Images the plan fixes for this resource: spec.image, spec.images, spec.containers[].image."""
        images: List[str] = []
        single = self.spec.get("image")
        if isinstance(single, str) and single.strip():
            images.append(single.strip())
        for image in self.spec.get("images") or []:
            if isinstance(image, str) and image.strip():
                images.append(image.strip())
        for key in ("containers", "initContainers"):
            for container in self.spec.get(key) or []:
                image = container.get("image") if isinstance(container, dict) else None
                if isinstance(image, str) and image.strip():
                    images.append(image.strip())
        return images

    def planned_replicas(self) -> Optional[int]:
        """Replica count the plan fixes, or None for kinds without replicas."""
        if self.type.lower() not in REPLICATED_KINDS:
            return None
        return self.spec.get("replicas", 1)


class Plan(BaseModel):
    resources: List[Resource] = Field(min_length=1, description="List of resources to be created")

# Files contract is a simple dictionary: dict[filename, yaml_text]
Files = Dict[str, str]

class Violation(BaseModel):
    tool: Literal["checkov", "opa", "dry-run", "plan"] = Field(description="The tool that found the violation")
    rule_id: str = Field(min_length=1, description="The specific rule ID that failed")
    severity: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"] = Field(description="Severity of the violation")
    file: Optional[str] = Field(None, description="The file where the violation occurred")
    line: Optional[int] = Field(None, description="The line number")
    message: str = Field(description="Description of the issue")
    resource: Optional[str] = Field(None, description="The resource involved")

class RunState(BaseModel):
    request_id: str
    user_role: str
    user_prompt: str
    plan: Optional[Plan] = None
    files: Optional[Files] = None
    violations: List[Violation] = []
    iterations: int = Field(0, ge=0)  # fix rounds performed
    history: List[dict] = [] # To store conversation/LLM history
    metrics: dict = {
        "guardrails_time": 0.0,
        "planning_time": 0.0,
        "generation_time": 0.0,
        "validation_time": 0.0,
        "fix_time": 0.0,
        "pr_time": 0.0,
        "llm_time": 0.0,
        "total_time": 0.0,
        "tokens_in": 0,
        "tokens_out": 0,
        "calls": 0
    }
    model: str
    status: Literal["running", "passed", "escalated", "rejected"] = "running"
    rejection_reason: Optional[str] = None
    pr_url: Optional[str] = None
    # Run log fields
    approved_plan: Optional[Plan] = None   # the plan the user confirmed; the Gauntlet's reference
    rounds: List[dict] = []                # one entry per Gauntlet run: round, files, violations, tool_seconds
    llm_calls: List[dict] = []             # one entry per LLM call: stage, tokens_in, tokens_out, seconds, ok
    tool_versions: dict = {}
    skipped_checks: List[str] = []
    temperature: Optional[float] = None
    commit: Optional[str] = None
    started_at: Optional[str] = None
    arm: Optional[str] = None
    pr_error: Optional[str] = None
    deploy: dict = {}
    guardrails_bypassed: bool = False      # benchmark correctness runs only; recorded in the log
    plan_source: str = "llm"                # "llm", or "shared" when the benchmark supplies the plan
    benchmark: dict = {}                   # benchmark id, tier, arm, repeat
