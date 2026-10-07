"""Runs the graph for the UI, the API and the benchmark, and writes the run log.

A run goes in two steps unless auto_approve is set: start() stops when the plan
is ready, and resume() continues once the user has confirmed or declined it.
"""
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.types import Command

from contracts import Plan, RunState
from graph import create_graph, default_services
from storage import save_run, git_commit

StepCallback = Optional[Callable[[str, RunState], None]]


class Pipeline:
    def __init__(self, llm=None, validator=None, gitops=None, auto_approve: bool = False, run_dir: str = "runs"):
        if llm is None or validator is None or gitops is None:
            default_llm, default_validator, default_gitops = default_services()
            llm = llm or default_llm
            validator = validator or default_validator
            gitops = gitops or default_gitops
        self.llm, self.validator, self.gitops = llm, validator, gitops
        self.run_dir = run_dir
        self.graph = create_graph(
            llm, validator, gitops, auto_approve=auto_approve,
            # The paused run is restored from the checkpoint; name the contract types it may rebuild.
            checkpointer=MemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=[
                ("contracts", name) for name in ("Plan", "Resource", "Violation", "RunState")
            ])),
        )

    def _config(self, request_id: str) -> dict:
        return {"configurable": {"thread_id": request_id}, "recursion_limit": 40}

    def _drive(self, request_id: str, graph_input, on_step: StepCallback) -> RunState:
        config = self._config(request_id)
        for update in self.graph.stream(graph_input, config, stream_mode="updates"):
            for step_name in update:
                if on_step and not step_name.startswith("__"):
                    on_step(step_name, self.state(request_id))
        state = self.state(request_id)
        if not self.awaiting_approval(request_id):
            save_run(state, self.run_dir)
        return state

    def state(self, request_id: str) -> RunState:
        return RunState.model_validate(self.graph.get_state(self._config(request_id)).values)

    def awaiting_approval(self, request_id: str) -> bool:
        return "approve" in self.graph.get_state(self._config(request_id)).next

    def start(self, prompt: str, role: str, request_id: Optional[str] = None, arm: Optional[str] = None,
              on_step: StepCallback = None, plan: Optional[Plan] = None, bypass_guardrails: bool = False,
              benchmark: Optional[dict] = None) -> RunState:
        """Runs until the plan needs approval, or to the end if the run finishes first."""
        state = RunState(
            request_id=request_id or uuid.uuid4().hex[:8],
            user_role=role,
            user_prompt=prompt,
            model=getattr(self.llm, "model_name", "unknown"),
            temperature=getattr(self.llm, "temperature", None),
            tool_versions=self.validator.tool_versions(),
            skipped_checks=self.validator.skipped_checks(),
            commit=git_commit(),
            started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            arm=arm,
            plan=plan,
            guardrails_bypassed=bypass_guardrails,
            benchmark=benchmark or {},
        )
        return self._drive(state.request_id, state, on_step)

    def resume(self, request_id: str, approved: bool, on_step: StepCallback = None) -> RunState:
        """Continues a run that is waiting at the approval step."""
        if not self.awaiting_approval(request_id):
            raise ValueError(f"run {request_id} is not waiting for approval")
        return self._drive(request_id, Command(resume={"approved": bool(approved)}), on_step)
