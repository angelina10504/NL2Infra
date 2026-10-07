from typing import Dict, List, Optional
from contracts import Plan, Resource, Violation


class MockLLMService:
    """Stands in for LLMService in MOCK_MODE. Makes no network calls and reports no tokens."""
    model_name = "mock"
    temperature = 0.1

    def drain_calls(self) -> List[dict]:
        return []

    def plan(self, prompt: str, role: str) -> Plan:
        return Plan(resources=[
            Resource(type="Deployment", name="mock-deploy", namespace="dev", spec={"image": "nginx:1.27-alpine", "replicas": 1}),
            Resource(type="Service", name="mock-svc", namespace="dev", spec={"port": 80})
        ])

    def generate(self, plan: Plan, role: str = "junior_dev") -> Dict[str, str]:
        return {
            "deployment-mock-deploy.yaml": (
                "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: mock-deploy\n  namespace: dev\n"
                "spec:\n  replicas: 1\n  selector:\n    matchLabels:\n      app: mock\n  template:\n"
                "    metadata:\n      labels:\n        app: mock\n    spec:\n      containers:\n"
                "        - name: mock\n          image: nginx:1.27-alpine\n"
            ),
            "service-mock-svc.yaml": (
                "apiVersion: v1\nkind: Service\nmetadata:\n  name: mock-svc\n  namespace: dev\n"
                "spec:\n  selector:\n    app: mock\n  ports:\n    - port: 80\n"
            ),
        }

    def fix(self, files: Dict[str, str], violations: List[Violation], plan: Optional[Plan] = None, **kwargs) -> Dict[str, str]:
        # Just return the same files in mock mode
        return dict(files)


class MockValidators:
    def __init__(self):
        self.called = False
        self.last_timings: Dict[str, float] = {}

    def tool_versions(self) -> Dict[str, str]:
        return {"mock": "mock"}

    def skipped_checks(self) -> List[str]:
        return []

    def validate(self, files: Dict[str, str], plan: Plan, role: str = "junior_dev") -> List[Violation]:
        # Return a fake violation on the first try, then empty
        if not self.called:
            self.called = True
            return [Violation(tool="checkov", rule_id="CKV_K8S_1", severity="HIGH", message="Mock violation",
                              file="deployment-mock-deploy.yaml")]
        return []


class MockGitOps:
    def commit_and_pr(self, files: Dict[str, str], request_id: str, environment: str = "dev") -> str:
        return f"https://github.com/mock/repo/pull/{request_id}"
