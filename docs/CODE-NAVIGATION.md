# Code Navigation

One-sentence summary: read-only Python navigation through pinned Pyright 1.1.411,
owned by the MCP process, freshness-proven, and never claiming market superiority.

## Trust and sandbox

Code navigation runs only against **trusted local repositories**. This is
**not an OS sandbox**. Pyright still runs with the current user's OS permissions
and may read configured interpreters, external stubs, and library code; those
inputs become fingerprinted provenance. Do not claim Pyright cannot write or read
other user-accessible paths.

## Installation

Pyright is installed by one explicit operator command into the approved managed
root `cache/code-tools/pyright/1.1.411/`. It **never downloads during a query**:

```bash
uv run python scripts/install_pyright.py --state-root "$LLM_WIKI_STATE_ROOT"
```

The installer verifies the pinned SHA-256 and npm integrity before publishing.
No query, MCP call, doctor check, or profile discovery path downloads or updates
Pyright. The qualified runtime uses Node 22; CI pins Node 22.23.1.

## Process ownership boundaries

- **Windows Job Object** owns the assigned Pyright server tree.
- **POSIX process group** covers the trusted, pinned Pyright server and
  descendants only while they remain in that group.
- A hostile `setsid()` escape is **unsupported**. This path is qualified only for
  pinned Pyright in trusted repositories and does not add an ancestry scan.

## get_architecture modes

Existing structural modes (`summary`, `symbol`, `callers`, `callees`,
`dependencies`, `path`, `community`, `impact`) retain their prior behavior and
the 10-second deadline.

Precise modes (`definition`, `references`, `implementations`, `type`,
`diagnostics`) and positioned `callers`/`callees` (with `path`, `line`, and
`character`) route through the owned Pyright session with one absolute 60-second
deadline created before validation.

Input positions are **one-based lines** and **zero-based UTF-8 byte offsets**.

## Status semantics

- `ok`: completed against one unchanged revision; empty provider result is still
  provider-reported, not closed-world proof.
- `partial`: useful facts exist but readiness, truncation, fallback, or timeout
  prevents a complete claim.
- `unsupported`: the provider did not advertise the capability.
- `not_ready`: startup or the readiness probe did not complete.
- `stale`: the workspace changed across both the attempt and the one retry.
- `timeout`: the operation deadline elapsed after cancellation.
- `error`: validated execution failed without a safe semantic result.

## Positions, deadlines, and offsets

- Positions are repository-relative; absolute roots and external paths are never
  exposed.
- Offsets are stateless: each offset reruns the request against a fresh current
  revision.
- Structural fallback is explicit, provenance-bearing, and appended after LSP
  results. Graph top-K is never used as an LSP filter.

## Synchronization and freshness

- Create, edit, rename, and delete changes are synchronized against one captured
  workspace revision before a provider request.
- The facade verifies the same revision after the request. A mismatch discards the
  attempt and retries once from a fresh revision; a second mismatch returns `stale`.
- Source-document parsing has a bounded revision-keyed LRU. Semantic/provider results
  are never cached.
- Query-time LSP facts are not published into Evidence Graph or any active generation.
- Empty provider results do not prove repository-wide absence. Complete negative
  answers remain unsupported.

## Bounds

- Default limit 10, maximum 100.
- At most 1,200 estimated tokens in default output.
- 8 MiB frames, 32 pending requests, 10,000 normalized locations, 10,000
  diagnostics, 256 KiB hover, 4 MiB stderr, JSON depth 64.

## Doctor codes and retention

Doctor reports stable codes: `pyright_missing`, `pyright_version_mismatch`,
`pyright_package_mismatch`, `pyright_executable_mismatch`, `pyright_node_mismatch`,
`pyright_initialization_mismatch`, `pyright_configuration_mismatch`,
`lsp_owner_live`, `lsp_failure_evidence_retained`, `lsp_state_unreadable`.

Process scratch lives under `run/lsp/<owner-nonce>/` and follows the existing
`run/` deletion contract, which protects live LSP owners and retained LSP
failure evidence for seven days. Doctor never installs or downloads Pyright and
never removes `run/lsp`.

## Qualification

The deterministic 100 KLOC qualification corpus and gates live under
`benchmark/`. It executes 200 definition, 100 reference, 100 call, 50 mutation,
20 recovery, and four ownership scenarios. Linux Python 3.10 additionally gates
warm facade overhead at 20 ms p95, cold readiness at 60 seconds, and client RSS below
100 MiB. Correctness-only real-Pyright checks run on Windows, Linux, and macOS.
**Market superiority remains unclaimed.**
