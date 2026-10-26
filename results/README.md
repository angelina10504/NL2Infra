# Benchmark & evaluation results

This folder holds evaluation output. **Be honest about what is real:** the numbers below are
either computed deterministically (no model, reproducible anywhere) or require credentials and
tools that were not available in the build environment.

## What is real and reproducible right now

### Guardrails (deterministic — no LLM, no cluster)
See [`guardrails.txt`](./guardrails.txt), produced by `python eval/evaluate.py`:

- **Refusal rate on adversarial requests: 45% (18/40).**
- **False-refusal rate on legitimate requests: 0% (0/30).**

**Interpretation (important, and deliberately honest).** The input guardrails are designed to
stop *input-layer* attacks: prompt injection, privilege escalation (wrong role/environment),
off-topic requests and direct-cluster-access attempts. They block all 18 such requests in the
adversarial set and falsely refuse none of the 30 legitimate requests.

The other 22 adversarial requests are *manifest-level* attacks — privileged containers, `hostPath`
mounts, `:latest` tags, missing resource limits, wrong `apiVersion`, plaintext secrets. These are
**intentionally let through the guardrails** because catching them is the job of the Gauntlet
(Checkov + OPA + `kubectl --dry-run` + plan-conformance) at validation time, not the input filter.
So a "guardrail refusal rate" of 45% is the *correct* figure for this two-layer design — it is not
the whole system's safety rate. (The static tiles in the HTML dashboard showing "40/40 blocked" are
illustrative placeholders, not measured values.)

## What still needs a real run (requires GROQ_API_KEY + checkov/conftest/kubectl)

The full four-arm LLM benchmark (A plain, B rules-in-prompt, C full pipeline, D pipeline+RAG),
70 requests × 3 repeats, was **not** run here because this environment has no model API key and no
scanner/cluster binaries. To produce it:

```bash
# 1. Put a real key in .env:  GROQ_API_KEY=...   MOCK_MODE=false
# 2. Install the scanners and a kind cluster (see the README quick start).
# 3. Run the four arms with 3 repeats:
python benchmarks/run_benchmark.py --yes --repeats 3 --arm all     # arms A, B, C
python benchmarks/run_benchmark.py --yes --repeats 3 --arm D       # arm D (RAG), reuses the shared plans
# 4. Optional second model: set MODEL_NAME to a free alternative and re-run into a second --out folder.

# 5. Results table and charts:
python eval/evaluate.py --runs runs/benchmark
python eval/charts.py  --runs runs/benchmark --out results/charts
```

`eval/evaluate.py` prints the full table (pass rate, violations per request, fix-loop rounds,
tokens, runtime, plan conformance) for every arm present in the logs, and `eval/charts.py` writes
the comparison PNGs into `results/charts/`. Report whatever numbers come out — do not tune the data.
