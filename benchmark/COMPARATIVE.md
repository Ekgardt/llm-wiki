# Task 27 Comparative Harness

The ordinary test suite runs only deterministic adapter integration fixtures. It does not download Graphify, load a model, index an external repository, or produce a public comparison claim.

## Adapter protocol

Each manifest command is one complete adapter for exactly one of these systems:

- `grep-read`: bounded lexical grep and source reads.
- `graphify-pinned`: Graphify commit `cb96bdaa0c367bec8d5c5aee5d7c9ebb727e9780`.
- `llm-wiki-current`: the current LLM Wiki search path.
- `evidence-graph-only`: LLM Wiki retrieval with the `GRAPH` profile.
- `hybrid-retrieval`: LLM Wiki retrieval with the `HYBRID` profile.
- `adaptive-context-compiler`: retrieval followed by the Adaptive Context Compiler.

The harness sends one `comparative-adapter-input/v1` JSON object on stdin. It includes the adapter backend/profile, task, seed, attempt, repository, commit, hardware, model, context budget, and retry policy. The command must write one closed object with `outcome`, `failure`, and all 14 Task27 `metrics` fields to stdout. A failed command, timeout, oversized output, malformed JSON, or missing metric becomes a raw failure ledger rather than disappearing from averages.

Commands run without a shell. Each attempt has the manifest timeout and stdout/stderr limits. The harness retains every retry and uses the terminal attempt for quality while counting tokens from every attempt.

## Deterministic fixture

From the repository root:

```powershell
uv run python benchmark/run_comparative.py --fixture `
  --manifest benchmark/fixtures/comparative-run-v1.json `
  --output D:\caches\temp\opencode\comparative-fixture `
  --json
```

Use a new or empty output directory. The output contains `report.json`, `artifact-index.json`, and one hashed JSON file per adapter attempt under `raw-task-ledgers/`. The fixture intentionally retries one Graphify attempt. Its report always has `quality_claim: false`.

## Exact real preflight

Prepare the external repository, Graphify checkout, model, adapter commands, environment variables, and GateF evidence yourself. The harness never installs or downloads them. Use absolute paths in a real manifest and run:

```powershell
uv run python benchmark/run_comparative.py --preflight `
  --manifest D:\absolute\comparative-run-v1.json `
  --json
```

Preflight returns exit code 0 only when all of these checks pass:

- CPython is exactly `3.12.10`.
- The target repository `HEAD` equals the declared 40-character commit.
- Graphify `HEAD` equals `cb96bdaa0c367bec8d5c5aee5d7c9ebb727e9780`.
- Graphify `uv.lock` has Git blob ID `088ebbbdcb17eacec5b60541f290381f6adf33e7`.
- The model probe command exists, exits successfully, and prints exactly the declared model ID.
- Every adapter command and required environment variable is available.
- The manifest uses the frozen seeds `[1729, 2718, 31415]`.
- GateF and all crash, evidence, and freshness hard gates are explicitly passed.
- The GateF evidence file hash matches and every referenced evidence artifact hash verifies.

The GateF evidence file is a closed object:

```json
{
  "artifacts": [
    {"path": "gate-f-test-report.json", "sha256": "<64 lowercase hex>"}
  ],
  "checks": {
    "deletes_and_renames_correct": true,
    "graph_tools_use_active_generation_or_explicit_fallback": true,
    "impact_preserves_uncertainty": true,
    "incremental_equals_clean_rebuild": true
  },
  "passed": true,
  "schema_version": "gate-f-evidence/v1"
}
```

Artifact paths may be absolute or relative to the GateF evidence file.

After an exact preflight succeeds, run the heavy comparison explicitly:

```powershell
uv run python benchmark/run_comparative.py --run `
  --manifest D:\absolute\comparative-run-v1.json `
  --output D:\absolute\new-comparative-evidence `
  --json
```

The public claim gate remains closed unless paired evidence is complete, the frozen confidence bounds pass strictly, GateF passes, and all three hard gates pass. Do not publish fixture output or partial real output as comparative evidence.

## Python code navigation qualification

A separate, deterministic qualification corpus lives at
`benchmark/code-navigation-python-v1.json` with its closed schema
`benchmark/code-navigation-python-v1.schema.json`. The generator
`benchmark/generate_python_qualification.py` emits a deterministic 100,000-line
Python repository seeded at `411` against pinned Pyright `1.1.411`. It never
invokes Git, Pyright, package installation, or the network.

Run the correctness-only fixture gate:

```powershell
uv run python benchmark/run_code_navigation.py --fixture --correctness-only --require-gates
```

The full qualification adds latency, token, and RSS gates on Linux:

```powershell
uv run python benchmark/run_code_navigation.py --fixture --qualification --require-gates
```

Acceptance requires: definitions at least 99%, reference F1 at least 95%, zero
stale answers, zero orphan processes inside the platform-qualified ownership
boundary, 100% bounded recovery, at most 10 default items and 1,200 estimated
tokens, warm overhead at most 20 ms at p95, cold readiness at most 60 seconds,
and client peak RSS below 100 MiB excluding the Pyright process tree.

Market superiority remains unclaimed. Comparative measurements are recorded only
against the named pinned releases, and the public result stays unclaimed until a
separately approved paired benchmark passes the predefined gates.
