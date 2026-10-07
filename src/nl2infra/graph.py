import os
import sys
from collections import Counter

# Ensure module path resolution
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from langgraph.graph import StateGraph, END
from langgraph.errors import GraphBubbleUp
from langgraph.types import interrupt

from contracts import RunState, Violation, CLUSTER_SCOPED_KINDS
from guardrails import check_guardrails
from llm_service import LLMError
from timing import track_time
from validators import is_tool_crash

MAX_FIX_ROUNDS = 5


def default_services():
    """Real services, or the mocks when MOCK_MODE=true. Unset means real, so a missing .env cannot fake a result."""
    if os.environ.get("MOCK_MODE", "false").lower() == "true":
        from mocks import MockLLMService, MockValidators, MockGitOps
        return MockLLMService(), MockValidators(), MockGitOps()
    from llm_service import LLMService
    from validators import Validators
    from gitops import GitOpsService
    return LLMService(), Validators(), GitOpsService()


def create_graph(llm=None, validator=None, gitops=None, auto_approve: bool = False, checkpointer=None):
    """Builds the pipeline:

    guardrails -> plan -> approve -> generate -> gauntlet -> fix (back to gauntlet,
    at most MAX_FIX_ROUNDS times) -> pull_request -> deploy

    Every failure ends the run in a named status; no node lets an exception escape.
    Without auto_approve the graph pauses at `approve` and needs a checkpointer.
    """
    if llm is None or validator is None or gitops is None:
        default_llm, default_validator, default_gitops = default_services()
        llm = llm or default_llm
        validator = validator or default_validator
        gitops = gitops or default_gitops

    def node(timer_key, func):
        """Runs func(state), which mutates state, and returns the whole state as the update."""
        def wrapped(state: RunState):
            try:
                with track_time(state.metrics, timer_key):
                    func(state)
            except GraphBubbleUp:
                raise  # the approval pause, not an error
            except LLMError as error:
                state.status = "escalated"
                state.rejection_reason = f"{error.reason}: {error}"
            except Exception as error:
                state.status = "escalated"
                state.rejection_reason = f"internal_error: {type(error).__name__}: {error}"
            finally:
                calls = llm.drain_calls()
                if calls:
                    state.llm_calls = state.llm_calls + calls
                    state.metrics["calls"] = len(state.llm_calls)
                    state.metrics["tokens_in"] = sum(c.get("tokens_in") or 0 for c in state.llm_calls)
                    state.metrics["tokens_out"] = sum(c.get("tokens_out") or 0 for c in state.llm_calls)
                    state.metrics["llm_time"] = round(sum(c.get("seconds") or 0 for c in state.llm_calls), 3)
            return {name: getattr(state, name) for name in RunState.model_fields}
        return wrapped

    def guardrails(state: RunState):
        if state.guardrails_bypassed:
            return  # benchmark correctness runs measure generation, not guardrails; the flag is in the run log
        reason = check_guardrails(state)
        if reason:
            state.status = "rejected"
            state.rejection_reason = reason

    def plan(state: RunState):
        if state.plan is not None:
            state.plan_source = "shared"  # supplied by the benchmark so every arm is scored against one plan
            return
        state.plan = llm.plan(state.user_prompt, state.user_role)

    def approve(state: RunState):
        if auto_approve:
            approved = True
        else:
            decision = interrupt({"request_id": state.request_id, "plan": state.plan.model_dump()})
            approved = decision.get("approved") is True if isinstance(decision, dict) else decision is True
        if not approved:
            state.status = "rejected"
            state.rejection_reason = "plan_not_approved: the user did not confirm the plan"
            return
        state.approved_plan = state.plan.model_copy(deep=True)

    def generate(state: RunState):
        state.files = llm.generate(state.approved_plan, role=state.user_role)

    def gauntlet(state: RunState):
        state.violations = validator.validate(state.files, state.approved_plan, role=state.user_role)
        state.rounds = state.rounds + [{
            "round": state.iterations,
            "files": dict(state.files),
            "violations": [v.model_dump() for v in state.violations],
            "tool_seconds": dict(getattr(validator, "last_timings", {})),
            "scan": dict(getattr(validator, "last_scan", {})),
        }]
        if is_tool_crash(state.violations):
            crashed = [v for v in state.violations if v.rule_id == "TOOL_CRASH"]
            state.status = "escalated"
            state.rejection_reason = "tool_crash: " + "; ".join(f"{v.tool}: {v.message}" for v in crashed)
        elif not state.violations:
            state.status = "passed"
        elif state.iterations >= MAX_FIX_ROUNDS:
            state.status = "escalated"
            state.rejection_reason = (
                f"max_fix_rounds: {len(state.violations)} violation(s) remain after {MAX_FIX_ROUNDS} fix rounds"
            )

    def fix(state: RunState):
        previous = state.rounds[-2] if len(state.rounds) >= 2 else None
        state.iterations += 1
        state.files = llm.fix(
            state.files, state.violations, plan=state.approved_plan,
            previous_files=previous["files"] if previous else None,
            previous_violations=[Violation(**v) for v in previous["violations"]] if previous else None,
            round_number=state.iterations,
        )

    def pull_request(state: RunState):
        namespaces = Counter(
            r.namespace for r in state.approved_plan.resources
            if r.type.lower() not in CLUSTER_SCOPED_KINDS and r.namespace
        )
        environment = namespaces.most_common(1)[0][0] if namespaces else "dev"
        try:
            url = gitops.commit_and_pr(state.files, state.request_id, environment=environment)
        except Exception as error:
            # The files passed; only publishing failed. The run stays passed and the PR step can be retried.
            state.pr_error = f"{type(error).__name__}: {error}"
            return
        if isinstance(url, str) and url.startswith(("https://", "http://")):
            state.pr_url = url
        else:
            state.pr_error = str(url)

    def deploy(state: RunState):
        # Nothing is applied here: a person merges the pull request and ArgoCD syncs the manifests repo.
        state.deploy = {"status": "awaiting_merge" if state.pr_url else "not_published"}

    workflow = StateGraph(RunState)
    workflow.add_node("guardrails", node("guardrails_time", guardrails))
    workflow.add_node("plan", node("planning_time", plan))
    workflow.add_node("approve", node("approval_time", approve))
    workflow.add_node("generate", node("generation_time", generate))
    workflow.add_node("gauntlet", node("validation_time", gauntlet))
    workflow.add_node("fix", node("fix_time", fix))
    workflow.add_node("pull_request", node("pr_time", pull_request))
    workflow.add_node("deploy", node("deploy_time", deploy))

    def proceed(next_node):
        """Go on only while the run is still running; any final status ends the graph."""
        return lambda state: next_node if state.status == "running" else END

    def after_gauntlet(state: RunState):
        if state.status == "passed":
            return "pull_request"
        return "fix" if state.status == "running" else END

    workflow.set_entry_point("guardrails")
    workflow.add_conditional_edges("guardrails", proceed("plan"), {"plan": "plan", END: END})
    workflow.add_conditional_edges("plan", proceed("approve"), {"approve": "approve", END: END})
    workflow.add_conditional_edges("approve", proceed("generate"), {"generate": "generate", END: END})
    workflow.add_conditional_edges("generate", proceed("gauntlet"), {"gauntlet": "gauntlet", END: END})
    workflow.add_conditional_edges(
        "gauntlet", after_gauntlet, {"pull_request": "pull_request", "fix": "fix", END: END}
    )
    workflow.add_conditional_edges("fix", proceed("gauntlet"), {"gauntlet": "gauntlet", END: END})
    workflow.add_edge("pull_request", "deploy")
    workflow.add_edge("deploy", END)

    return workflow.compile(checkpointer=checkpointer)
