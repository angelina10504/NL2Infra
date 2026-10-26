"""RAG tests: the knowledge base builds, retrieval returns the right policy for plain-English
queries, the generate prompt gets grounded (arm D), and the 'why?' bot explains run logs.

All offline: TF-IDF embeddings, no model download, no network."""
import json
import os

import pytest

from contracts import Resource
from rag import answer_why, build_documents, build_runlog_documents
from rag.retriever import Retriever, clear_cache


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_cache()
    yield
    clear_cache()


def test_knowledge_base_has_policy_and_standards_documents():
    docs = build_documents(runs_dir=None, include_examples=False)
    sources = {d["source"] for d in docs}
    assert "policy" in sources and "standards" in sources
    rule_ids = {d["metadata"].get("rule_id") for d in docs if d["source"] == "policy"}
    assert {"OPA_NO_ROOT", "OPA_DISALLOW_LATEST_TAG", "OPA_REQUIRE_CPU_LIMIT"} <= rule_ids


@pytest.mark.parametrize("query,expected_rule", [
    ("container is running as root user", "OPA_NO_ROOT"),
    ("image uses the latest tag", "OPA_DISALLOW_LATEST_TAG"),
    ("missing cpu limit on the container", "OPA_REQUIRE_CPU_LIMIT"),
    ("memory limit not set", "OPA_REQUIRE_MEM_LIMIT"),
    ("plaintext password in an environment variable", "OPA_NO_PLAINTEXT_SECRET"),
    ("privileged container", "OPA_NO_PRIVILEGED"),
    ("needs a readiness probe", "OPA_REQUIRE_READINESS_PROBE"),
    ("production needs at least two replicas", "OPA_PRODUCTION_REPLICAS"),
])
def test_retrieval_returns_the_right_policy(query, expected_rule):
    retriever = Retriever(build_documents(runs_dir=None, include_examples=False))
    hits = retriever.search(query, k=3, sources=["policy"])
    assert expected_rule in {h["metadata"]["rule_id"] for h in hits}


def test_context_for_resource_grounds_with_policies():
    retriever = Retriever(build_documents(runs_dir=None, include_examples=False))
    resource = Resource(type="Deployment", name="web", namespace="prod",
                        spec={"image": "nginx:1.27-alpine", "replicas": 2})
    context = retriever.context_for_resource(resource)
    assert context and "OPA_" in context


def test_generate_resource_appends_retrieved_context():
    """LLMService with a retriever augments the generate prompt (captured, no network)."""
    from llm_service import LLMService

    captured = {}

    class FakeChat:
        def invoke(self, prompt):
            captured["prompt"] = prompt
            class R:
                content = "apiVersion: v1\nkind: Service\nmetadata:\n  name: web\n"
                usage_metadata = {"input_tokens": 10, "output_tokens": 5}
            return R()

    retriever = Retriever(build_documents(runs_dir=None, include_examples=False))
    svc = LLMService(llm=FakeChat(), model_name="fake", retriever=retriever)
    resource = Resource(type="Deployment", name="web", namespace="dev",
                        spec={"image": "nginx:1.27-alpine", "replicas": 1})
    svc.generate_resource(resource, "deployment-web.yaml", "developer")
    assert "retrieved for grounding" in captured["prompt"]
    assert "OPA_" in captured["prompt"]


def test_why_bot_explains_a_blocked_and_a_passed_run(tmp_path):
    # A blocked run and a passed run, written as run logs.
    (tmp_path / "r-blocked.json").write_text(json.dumps({
        "request_id": "r-blocked", "prompt": "deploy to production as a junior dev",
        "status": "rejected", "rejection_reason": "unauthorized: role may not target prod", "rounds": [],
    }))
    (tmp_path / "r-passed.json").write_text(json.dumps({
        "request_id": "r-passed", "prompt": "deploy an nginx service to dev", "status": "passed",
        "iterations": 1,
        "rounds": [
            {"round": 0, "files": {}, "violations": [{"rule_id": "OPA_NO_ROOT"}]},
            {"round": 1, "files": {}, "violations": []},
        ],
    }))

    blocked = answer_why("why was my production request rejected", runs_dir=str(tmp_path))
    assert "blocked by the guardrails" in blocked["answer"]
    assert any(s["request_id"] == "r-blocked" for s in blocked["sources"])

    changed = answer_why("why did my nginx manifests change", runs_dir=str(tmp_path))
    assert "OPA_NO_ROOT" in changed["answer"] or "fix loop" in changed["answer"]


def test_arm_d_pipeline_runs_end_to_end_with_grounding(tmp_path):
    """Arm D = full pipeline + RAG. Run it end-to-end with a fake chat model and fake Gauntlet."""
    from contracts import Plan
    from llm_service import LLMService
    from pipeline import Pipeline

    prompts = []

    class FakeChat:
        def invoke(self, prompt):
            prompts.append(prompt)
            class R:
                content = "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n  namespace: dev\n"
                usage_metadata = {"input_tokens": 20, "output_tokens": 8}
            return R()

    class FakeValidator:
        last_timings = {"checkov": 0.1}
        last_scan = {}
        def tool_versions(self): return {"checkov": "3", "conftest": "d", "opa": "1", "kubectl": "v1"}
        def skipped_checks(self): return []
        def validate(self, files, plan, role="developer"): return []

    class FakeGitOps:
        def commit_and_pr(self, files, request_id, environment="dev"):
            return "https://github.com/acme/nl2infra-manifests/pull/1"

    retriever = Retriever(build_documents(runs_dir=None, include_examples=False))
    llm = LLMService(llm=FakeChat(), model_name="fake", retriever=retriever)
    pipeline = Pipeline(llm, FakeValidator(), FakeGitOps(), auto_approve=True, run_dir=str(tmp_path))
    plan = Plan.model_validate({"resources": [
        {"type": "Deployment", "name": "web", "namespace": "dev", "spec": {"image": "nginx:1.27-alpine", "replicas": 1}}
    ]})
    state = pipeline.start("Deploy nginx to dev", "developer", request_id="d1", plan=plan, arm="D",
                           bypass_guardrails=True)
    assert state.status == "passed"
    # The generate prompt was grounded with retrieved policies.
    assert any("retrieved for grounding" in p for p in prompts)


def test_runlog_documents_capture_fixed_rules(tmp_path):
    (tmp_path / "r.json").write_text(json.dumps({
        "request_id": "r", "prompt": "x", "status": "passed", "iterations": 1,
        "rounds": [
            {"round": 0, "files": {}, "violations": [{"rule_id": "CKV_K8S_23"}]},
            {"round": 1, "files": {}, "violations": []},
        ],
    }))
    docs = build_runlog_documents(str(tmp_path))
    assert docs and "CKV_K8S_23" in docs[0]["metadata"]["fixed"]
