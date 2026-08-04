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
The generated-tree identity hashes every Python source plus
`pyrightconfig.json`; `.git` metadata is excluded. The pinned 100,000-line tree
contains 48 Python files and 3,034,706 Python bytes, within a fixed 2 MiB to
4 MiB range. Its 32 padding modules use 64-line integer-accumulator functions:
every seed-derived addition contributes to the return value, and every later
function calls the prior block. `qual/padding_users.py` statically imports and
calls all 32 final entry functions. Definition queries `definition-168` through
`definition-199` bind those call sites to the exact entry definitions, making
every internal block transitively reachable from a real correctness query. A
generated Python catalog binds every mutation workload field and byte variant
into the pinned source identity.

The closed workload contains 400 gold queries and 50 mutation cycles. Each
cycle deterministically resets the same four shared workload paths, then
performs five definition checks from the stable, pre-existing probe file:
create positive, definition-changing edit positive, renamed-path positive,
old-path negative, and deleted-path negative. Reports therefore expose 250
freshness attempts plus explicit measured denominators and stale/orphan rates.
An empty rename-old or delete result counts only with provider status `OK`.
Positive exact, citation-valid answers accept `OK` or `PARTIAL` because the
qualified facade intentionally labels references and call hierarchy partial.

Both modes require an already-installed, qualified Pyright `1.1.411` package
with the pinned package digest and Node major `22`. The runner discovers the
artifact from `LLM_WIKI_STATE_ROOT` (or `--state-root`) and never downloads,
installs, or updates it.
One absolute 13-minute run budget caps discovery and every measured operation.
No operation can renew that budget. Runtime and operator cleanup use separate,
fresh 30-second deadlines with one retained-cleanup retry.
Timeout and cancellation ownership probes launch a real no-match
`workspace/symbol` request, require lock-protected proof that its frame reached
the sent phase, observe the expected in-flight terminal, and only then reset and
check process ownership.

The correctness and reliability fixture gates are cross-platform:

```powershell
uv run python benchmark/run_code_navigation.py --fixture --correctness-only --require-gates
```

The full qualification adds the paired latency and client-process RSS gates.
Those fixed performance gates are run on Linux with Python 3.10 by Task 15 CI;
runs on other platforms are diagnostic and are not Linux qualification evidence:

```powershell
uv run python benchmark/run_code_navigation.py --fixture --qualification --require-gates
```

Cross-platform correctness and reliability acceptance requires: definitions at
least 99%, reference F1 at least 95%, zero stale answers, zero orphan processes
inside the platform-qualified ownership boundary, 100% bounded recovery, at
most 10 default items and 1,200 estimated tokens. Task 15's Linux Python 3.10
qualification additionally requires warm overhead at most 20 ms at p95, cold
readiness at most 60 seconds, and client peak RSS below 100 MiB excluding the
Pyright process tree.

Qualification reports record `platform.system()` and the machine-readable
`platform.python_version()` value. Only exact `Linux` and a `3.10.<patch>`
identity can pass qualification gates; correctness-only reports remain
cross-platform. Warm timing uses 20 deterministic queries spread across the
gold ranges: 10 definitions, 5 references, and 5 calls. Each sample preserves
the counterbalanced direct/facade, facade/direct pair. Both raw direct results
are normalized with repository containment and the negotiated position
encoding and must match current gold bytes exactly; both facade results must be
exact and citation-valid. Raw-tool token estimates cover the full normalized
result without source text or raw LSP payloads. Incomplete samples, non-finite
values, inconsistent metric arithmetic, invalid task IDs, and final runtime or
operator-probe close failures keep evidence unavailable and gates closed.
Operator-corpus probing reports aggregate counts only. Its deterministic
traversal does not follow links or Windows reparse points, caps depth, scanned
entries, and selected Python files, and fails closed on a cap or deadline.
Each selected source is read through a no-follow descriptor in bounded 64 KiB
chunks with a fixed 1 MiB per-file ceiling and before/after identity checks.

Market superiority remains unclaimed. Comparative measurements are recorded only
against the named pinned releases, and the public result stays unclaimed until a
separately approved paired benchmark passes the predefined gates.
