"""The only code that calls the model.

The model has no tools: every call sends text and reads text back. Nothing here
binds tools or uses provider-side structured output.
"""
import os
import re
import json
import time
import logging
from typing import Callable, Dict, List, Optional
from dotenv import load_dotenv
from pydantic import ValidationError

from contracts import Plan, Resource, Violation, WORKLOAD_KINDS
from rules import rules_for_role

load_dotenv()
logger = logging.getLogger(__name__)

TEMPERATURE = 0.1
TIMEOUT_SECONDS = 30
BACKOFF_SECONDS = (2, 4, 8)      # retries after a timeout or 5xx
MAX_RATE_LIMIT_WAITS = 6         # HTTP 429 waits before giving up
DEFAULT_RATE_LIMIT_WAIT = 15.0
MAX_RATE_LIMIT_WAIT = 300.0


class LLMError(Exception):
    reason = "llm_error"


class LLMUnavailable(LLMError):
    """Timeout, HTTP error or exhausted rate-limit waits."""
    reason = "llm_unavailable"


class LLMBadOutput(LLMError):
    """The model answered, but not with something usable."""
    reason = "bad_llm_output"


def _load_prompt_template(filename: str) -> str:
    path = os.path.join(os.path.dirname(__file__), "prompts", filename)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _extract_json_block(text: str) -> Optional[dict]:
    """Helper to extract JSON object from markdown fenced blocks or raw strings."""
    text = text.strip()
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = match.group(1) if match else text

    # Try direct parse
    try:
        return json.loads(candidate)
    except Exception:
        # Try to locate outermost braces
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except Exception:
                pass
    return None


def _extract_yaml(text: str) -> str:
    """Returns the YAML in a reply: the fenced blocks if there are any, else the whole text."""
    blocks = re.findall(r"```[a-zA-Z]*[ \t]*\n(.*?)```", text, re.DOTALL)
    body = "\n---\n".join(b.strip("\n") for b in blocks if b.strip()) if blocks else text.strip()
    if not body.strip():
        raise LLMBadOutput("the model returned no YAML")
    return body.rstrip() + "\n"


def image_problem(image: str) -> Optional[str]:
    """Why an image reference cannot pass the Gauntlet, or None if it can."""
    if "@" in image:
        return None
    last = image.split("/")[-1]
    if ":" not in last:
        return f"image '{image}' has no tag"
    if last.endswith(":latest"):
        return f"image '{image}' uses the ':latest' tag"
    return None


def plan_problems(plan: Plan) -> List[str]:
    """Problems that would make zero violations unreachable, because the fixer may not change the plan."""
    problems = []
    seen = set()
    for res in plan.resources:
        key = (res.type.lower(), res.name.lower())  # one file and one conformance entry per kind/name
        if key in seen:
            problems.append(f"{res.type}/{res.name} is listed twice")
        seen.add(key)
        if res.type.lower() in WORKLOAD_KINDS:
            for image in res.planned_images():
                problem = image_problem(image)
                if problem:
                    problems.append(f"{res.type}/{res.name}: {problem}; use a specific version tag")
    return problems


def plan_filenames(plan: Plan) -> Dict[str, str]:
    """One file per planned resource: 'Kind/name' -> filename."""
    names: Dict[str, str] = {}
    for res in plan.resources:
        filename = f"{res.type.lower()}-{res.name.lower()}.yaml"
        names[f"{res.type}/{res.name}"] = re.sub(r"[^a-z0-9._-]", "-", filename)
    return names


def violations_by_file(files: Dict[str, str], violations: List[Violation], plan: Plan) -> Dict[str, List[Violation]]:
    """Groups violations by the file that has to change. A missing planned resource maps to its own file."""
    names = plan_filenames(plan)
    by_label = {label.lower(): filename for label, filename in names.items()}
    grouped: Dict[str, List[Violation]] = {}
    for v in violations:
        if v.file and v.file in files:
            targets = [v.file]
        elif v.resource and v.resource.lower() in by_label:
            targets = [by_label[v.resource.lower()]]
        else:
            targets = list(files) or list(names.values())  # cannot be attributed: every file sees it
        for target in targets:
            grouped.setdefault(target, []).append(v)
    return grouped


def _format_violations(violations: List[Violation]) -> str:
    return "\n".join(
        f"- {v.rule_id} ({v.tool}, {v.severity})" + (f", line {v.line}" if v.line else "") + f": {v.message}"
        for v in violations
    )


def _status_code(error: Exception) -> Optional[int]:
    for source in (error, getattr(error, "response", None)):
        code = getattr(source, "status_code", None)
        if isinstance(code, int):
            return code
    return None


def _classify(error: Exception) -> str:
    """'rate_limit', 'retryable' (timeout, 5xx, connection) or 'fatal'."""
    status = _status_code(error)
    name = type(error).__name__.lower()
    if status == 429 or "ratelimit" in name or "resourceexhausted" in name:
        return "rate_limit"
    if isinstance(error, (TimeoutError, ConnectionError)) or "timeout" in name or "connection" in name:
        return "retryable"
    if status is not None and status >= 500:
        return "retryable"
    return "fatal"


def _retry_after(error: Exception) -> float:
    headers = getattr(getattr(error, "response", None), "headers", None) or {}
    try:
        return min(max(float(headers.get("retry-after")), 0.0), MAX_RATE_LIMIT_WAIT)
    except (TypeError, ValueError):
        return DEFAULT_RATE_LIMIT_WAIT


def _usage(response) -> tuple:
    """(tokens_in, tokens_out) as reported by the provider, or None where it reported nothing."""
    usage = getattr(response, "usage_metadata", None)
    if isinstance(usage, dict) and usage.get("input_tokens") is not None:
        return usage.get("input_tokens"), usage.get("output_tokens")
    usage = (getattr(response, "response_metadata", None) or {}).get("token_usage") or {}
    return usage.get("prompt_tokens"), usage.get("completion_tokens")


def _text(response) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, list):
        content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return content if isinstance(content, str) else str(content)


class LLMService:
    def __init__(self, llm=None, model_name: Optional[str] = None, sleep: Callable[[float], None] = time.sleep):
        self.groq_api_key = os.environ.get("GROQ_API_KEY")
        self.gemini_api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        self.provider = os.environ.get("LLM_PROVIDER", "").lower()

        # Auto-detect provider if not explicitly given
        if not self.provider:
            if self.gemini_api_key and not self.gemini_api_key.startswith("your_") and not self.groq_api_key:
                self.provider = "gemini"
            else:
                self.provider = "groq"

        self.model_name = model_name or os.environ.get("MODEL_NAME") or (
            "gemini-1.5-pro" if self.provider == "gemini" else "llama-3.3-70b-versatile"
        )
        self.temperature = TEMPERATURE
        self.sleep = sleep
        self.call_log: List[dict] = []
        self.llm = llm if llm is not None else self._init_llm()

    def _init_llm(self):
        # max_retries=0: retries, backoff and rate-limit waits are handled in _call so they are logged.
        if self.provider == "gemini":
            from langchain_google_genai import ChatGoogleGenerativeAI
            return ChatGoogleGenerativeAI(
                model=self.model_name, google_api_key=self.gemini_api_key,
                temperature=self.temperature, timeout=TIMEOUT_SECONDS, max_retries=0,
            )
        from langchain_groq import ChatGroq
        return ChatGroq(
            model_name=self.model_name, groq_api_key=self.groq_api_key,
            temperature=self.temperature, request_timeout=TIMEOUT_SECONDS, max_retries=0,
        )

    def drain_calls(self) -> List[dict]:
        """Returns and clears the per-call log (stage, tokens, seconds, outcome)."""
        calls, self.call_log = self.call_log, []
        return calls

    def _call(self, prompt: str, stage: str, **context) -> str:
        """One text-in, text-out call with timeout, backoff and rate-limit handling. Every attempt is logged."""
        retries = 0
        rate_limit_waits = 0
        while True:
            started = time.time()
            record = {"stage": stage, **context, "model": self.model_name, "tokens_in": None, "tokens_out": None}
            try:
                response = self.llm.invoke(prompt)
            except Exception as error:
                kind = _classify(error)
                record.update(ok=False, seconds=round(time.time() - started, 3), error=f"{type(error).__name__}: {str(error)[:200]}",
                              http_status=_status_code(error))
                self.call_log.append(record)
                if kind == "rate_limit" and rate_limit_waits < MAX_RATE_LIMIT_WAITS:
                    wait = _retry_after(error)
                    rate_limit_waits += 1
                    record["waited_seconds"] = wait
                    logger.warning("Rate limited (HTTP 429); waiting %.1f s", wait)
                    self.sleep(wait)
                    continue
                if kind == "retryable" and retries < len(BACKOFF_SECONDS):
                    wait = BACKOFF_SECONDS[retries]
                    retries += 1
                    record["waited_seconds"] = wait
                    self.sleep(wait)
                    continue
                raise LLMUnavailable(f"{stage}: {type(error).__name__}: {str(error)[:200]}") from error

            tokens_in, tokens_out = _usage(response)
            record.update(ok=True, seconds=round(time.time() - started, 3), tokens_in=tokens_in, tokens_out=tokens_out)
            self.call_log.append(record)
            return _text(response)

    # ------------------------------------------------------------------ plan

    def plan(self, prompt: str, role: str) -> Plan:
        """Returns a validated Plan. One retry with the error in the prompt, then LLMBadOutput."""
        base_prompt = _load_prompt_template("plan_prompt.txt").format(role=role, prompt=prompt)
        llm_prompt = base_prompt
        error = ""
        for attempt in (1, 2):
            text = self._call(llm_prompt, "plan", attempt=attempt)
            parsed = _extract_json_block(text)
            if not isinstance(parsed, dict) or "resources" not in parsed:
                error = "the reply was not a JSON object with a 'resources' list"
            else:
                try:
                    plan = Plan.model_validate(parsed)
                    problems = plan_problems(plan)
                    if not problems:
                        return plan
                    error = "; ".join(problems)
                except ValidationError as e:
                    error = "; ".join(
                        f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()[:8]
                    )
            llm_prompt = (
                f"{base_prompt}\n\nYour previous reply was rejected: {error}\n"
                "Reply again with only the corrected JSON object."
            )
        raise LLMBadOutput(f"plan rejected twice: {error}")

    # -------------------------------------------------------------- generate

    def generate_resource(self, resource: Resource, filename: str, role: str) -> str:
        prompt = _load_prompt_template("generate_prompt.txt").format(
            resource_json=resource.model_dump_json(indent=2),
            rules=rules_for_role(role),
        )
        return _extract_yaml(self._call(prompt, "generate", file=filename))

    def generate(self, plan: Plan, role: str) -> Dict[str, str]:
        """One LLM call per planned resource, one YAML file each.

        Each prompt carries the full rule list for the role, the same one benchmark arm B gets.
        """
        names = plan_filenames(plan)
        return {
            names[f"{res.type}/{res.name}"]: self.generate_resource(res, names[f"{res.type}/{res.name}"], role)
            for res in plan.resources
        }

    # ------------------------------------------------------------------- fix

    def fix(self, files: Dict[str, str], violations: List[Violation], plan: Plan,
            previous_files: Optional[Dict[str, str]] = None,
            previous_violations: Optional[List[Violation]] = None, round_number: int = 0) -> Dict[str, str]:
        """Sends only the failing files back, one call each, and returns the full updated file set.

        If a rule failed on the same file in the previous round too, the previous
        attempt is included so the model does not repeat it.
        """
        template = _load_prompt_template("fix_prompt.txt")
        names = plan_filenames(plan)
        resource_for_file = {names[f"{r.type}/{r.name}"]: r for r in plan.resources}
        failing = violations_by_file(files, violations, plan)
        earlier = violations_by_file(previous_files or {}, previous_violations or [], plan) if previous_files else {}

        updated = dict(files)
        for filename, file_violations in failing.items():
            resource = resource_for_file.get(filename)
            repeated = sorted({v.rule_id for v in file_violations} & {v.rule_id for v in earlier.get(filename, [])})
            failed_attempt = ""
            if repeated and previous_files and filename in previous_files:
                failed_attempt = (
                    f"\nThese rules also failed in the previous round: {', '.join(repeated)}.\n"
                    "Your last fix did not work. This was the file before that fix; the current file above is "
                    "your attempt. Both fail, so do something different:\n"
                    f"```yaml\n{previous_files[filename].rstrip()}\n```\n"
                )
            prompt = template.format(
                filename=filename,
                current_manifest=files.get(filename, "").rstrip() or "# (the file is missing or empty)",
                violations_text=_format_violations(file_violations),
                resource_json=resource.model_dump_json(indent=2) if resource else "This file is not in the plan and must be emptied of unplanned resources.",
                failed_attempt=failed_attempt,
            )
            updated[filename] = _extract_yaml(self._call(
                prompt, "fix", file=filename, round=round_number, repeated_rules=repeated
            ))
        return updated
