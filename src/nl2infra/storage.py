import json
import os
import subprocess
from typing import Optional

from contracts import RunState

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def git_commit() -> Optional[str]:
    """Commit of the app code, or None when the folder is not a git repository."""
    try:
        proc = subprocess.run(
            ["git", "-C", _REPO_ROOT, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10
        )
    except Exception:
        return None
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None


def run_log(state: RunState) -> dict:
    """The run log: everything in RunState, plus the names the evaluation reads."""
    data = json.loads(state.model_dump_json())
    data.update({
        "role": state.user_role,
        "prompt": state.user_prompt,
        "model_id": state.model,
        "fix_rounds": state.iterations,
        "final_status": state.status,
        "date": state.started_at,
        "stage_timings": {k: v for k, v in state.metrics.items() if k.endswith("_time")},
    })
    return data


def save_run(state: RunState, run_dir: str = "runs") -> str:
    os.makedirs(run_dir, exist_ok=True)
    file_path = os.path.join(run_dir, f"{state.request_id}.json")
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(run_log(state), f, indent=2)
    return file_path


def load_runs(run_dir: str = "runs") -> list[RunState]:
    runs = []
    if not os.path.exists(run_dir):
        return runs
    for filename in sorted(os.listdir(run_dir)):
        if filename.endswith(".json"):
            with open(os.path.join(run_dir, filename), "r") as f:
                data = json.load(f)
                runs.append(RunState(**data))
    return runs
