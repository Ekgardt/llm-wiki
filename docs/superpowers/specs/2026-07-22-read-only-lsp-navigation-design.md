---
type: decision
title: "Read-Only LSP Navigation Engine Design"
date: 2026-07-22
confidence: high
source_authority: user
status: approved
---
# Read-Only LSP Navigation Engine Design

One-sentence summary: LLM Wiki will keep its existing structural Evidence Graph and add an owned, minimal, read-only LSP engine for precise live navigation, starting with production-quality Python support through Pyright.

## Status

This is an approved target, not implemented behavior. The current runtime remains
`corpus-generation/v2` with `evidence-graph/v2`. The completed foundation work in
Tasks 1-5 of the 2026-07-21 Plan A implementation remains in place. Tasks 6-16 of
that plan are superseded by this design and a new implementation plan.

## Goal

Deliver useful code intelligence now without building another product or another
persistent semantic index:

- use the existing Evidence Graph and Tree-sitter extraction for fast structural
  discovery, architecture, impact candidates, and fallback;
- use a small LLM Wiki-owned LSP runtime for precise position-addressed semantic
  operations;
- expose compact, progressively disclosed evidence through the existing 12 MCP
  tools;
- make Python/Pyright production-ready before adding another language family;
- preserve a measurable path to outperform codebase-memory-mcp and Graphify in
  task quality, precision, reliability, end-to-end latency, and tokens per solved
  task without claiming that outcome before paired evidence exists.

## Non-Goals

- No Serena or SolidLSP runtime dependency.
- No separate MCP server, graph store, catalog, active pointer, runtime root, or
  persistent daemon.
- No Rust rewrite.
- No mandatory SCIP pipeline in the interactive path.
- No full-project semantic export through LSP.
- No query-time publication into Evidence Graph v3.
- No automatic language-server installation during a query.
- No mutation methods such as rename, formatting, code actions, completion, or
  arbitrary `workspace/executeCommand`.
- No claim of exact runtime call-graph completeness.

## Existing Boundaries Remain

LLM Wiki keeps:

- exactly 12 task-shaped MCP tools and the existing response envelope;
- the existing retrieval planner for source and knowledge retrieval;
- the existing Context Compiler for broad, multi-source synthesis;
- Evidence Graph generations, FTS, graph traversal, and Tree-sitter structural
  evidence;
- Markdown, Git, project journals, live source bytes, and accepted artifacts as
  authority;
- Python 3.10 compatibility;
- the current `cache/`, `logs/`, and `run/` layout.

The LSP engine is a live read adapter. It is not another authority or publication
boundary.

## Architecture

```text
AI agent
  -> existing LLM Wiki MCP tool and mode
  -> code navigation facade
       -> existing Evidence Graph / Tree-sitter for broad discovery
       -> owned read-only LSP runtime for precise live operations
  -> deterministic compact renderer
  -> Context Compiler only when broad code and knowledge evidence must be packed
```

### Code Navigation Facade

The rest of LLM Wiki depends on one stable internal contract rather than LSP
transport details. The target capabilities are:

- definition;
- references;
- implementations when the server advertises support;
- type definition and hover;
- incoming and outgoing calls when call hierarchy is supported;
- diagnostics;
- verification of one specific structural edge.

Every operation requires a repository root and either an exact file position or a
previously disambiguated symbol anchor. Name-only requests first use existing
structural or exact symbol search to locate candidate declarations. Ambiguity is
returned, not silently resolved.

The public MCP boundary remains `get_architecture`. Its existing `symbol`,
`callers`, and `callees` modes remain backward compatible. `callers` and `callees`
use precise navigation only when `path`, `line`, and `character` are supplied;
otherwise they retain structural behavior. New modes are `definition`,
`references`, `implementations`, `type`, and `diagnostics`. Precise modes require:

- `directory`: absolute repository root;
- `path`: repository-relative file path;
- `line`: one-based source line;
- `character`: zero-based UTF-8 byte offset on that source line;
- optional `offset` and `limit` for bounded result windows.

The facade validates the UTF-8 position against current source bytes and converts
it to the server-negotiated encoding. Every mode returns the existing MCP envelope
plus typed `ok`, `partial`, `unsupported`, `not_ready`, `stale`, `timeout`, or
`error` status. `offset` is stateless; the request is rerun against the current
workspace revision rather than retaining a hidden result cursor.

### Owned LSP Runtime

The runtime is implemented against LSP 3.18. Serena/SolidLSP and Microsoft
multilspy may be used as research references and behavioral comparison oracles,
but they are not runtime dependencies. Adapted MIT-licensed code, if any, retains
its notices and provenance.

The runtime owns only:

- stdio JSON-RPC framing and bounded request tracking;
- `initialize`, `initialized`, capability negotiation, cancellation, `shutdown`,
  and `exit`;
- URI and negotiated UTF-8/UTF-16/UTF-32 position normalization;
- document open/change/close synchronization;
- allowlisted server requests, progress, and diagnostics notifications;
- process ownership, timeouts, bounded restart, and process-tree cleanup;
- one small profile per supported language server.

It does not implement an IDE, package manager, downloader, editor, or language
semantics.

The protocol boundary accepts at most an 8 MiB frame, 32 outstanding requests,
10,000 normalized locations, 10,000 diagnostics, a 256 KiB hover payload, and a
4 MiB bounded stderr ring. JSON nesting deeper than 64 is rejected before result
normalization. Malformed frames, invalid headers, duplicate active response IDs,
or oversized payloads fail the request, terminate that server instance, and permit
at most one bounded restart. Late responses carry a process-generation nonce and
are dropped after cancellation or restart. Unknown server-to-client requests
receive `MethodNotFound`; only configuration, progress, capability registration,
and diagnostics methods are allowlisted.

### Lifecycle

Servers start lazily for the repository and language actually queried. Their
lifetime is bounded by the owning MCP process and an idle timeout; there is no
independent daemon. Startup states are explicit:

- `process_running`;
- `protocol_initialized`;
- `workspace_ready`;
- `degraded`;
- `failed`.

Receiving an `initialize` response does not prove workspace readiness. Each server
profile may observe progress. The Pyright profile becomes `query_ready` only after
successful initialization, configuration delivery, `didOpen` for the target file,
and a successful bounded `textDocument/documentSymbol` probe for that file. Its
startup deadline is 60 seconds. This proves that the target document is queryable,
not that cross-file indexing is complete; references and call results therefore
remain `provider_reported` unless stronger coverage evidence exists. A timeout
produces `not_ready` or `partial`, never a false complete negative.

Before a semantic request, the facade computes a workspace revision from Git HEAD
plus content hashes of dirty, untracked, deleted, and relevant Python configuration
files. Non-Git projects use a bounded content manifest. The revision diff drives
create/change/rename/delete synchronization and increments a workspace epoch.
After the response, the facade recomputes the revision. A mismatch discards the
response and retries once; a second mismatch returns `stale`. Every result carries
the pre/post revision and document version. This protocol, not watcher delivery,
is the freshness proof.

## Query Routing

### Exact Semantic Operations

Definition, references, implementations, type information, and call hierarchy go
directly to LSP when an exact position is available. The structural graph is a
fallback and an additional provenance-bearing observation, not a top-K filter on
the LSP result.

### Name-Only Discovery

The existing graph or exact symbol search locates declaration candidates. The
caller must disambiguate when multiple candidates remain. LSP then receives the
selected declaration position.

### References And Implementations

The full provider-reported LSP result is requested from the declaration position.
Graph-only observations may be added as candidates. The system must not verify
only the top graph candidates and present them as complete because that loses
recall.

### Calls

Call hierarchy is preferred when advertised. Otherwise references are classified
with existing structural evidence. `references` is never treated as synonymous
with `calls`. Dynamic dispatch, reflection, dependency injection, callbacks,
generated code, and macros remain explicitly uncertain.

### Architecture And Impact

Evidence Graph remains the primary path for architecture, communities, bounded
paths, and broad impact discovery. LSP may confirm selected high-value edges. A
confirmed sample does not make the whole graph complete.

### Dead Code

The graph proposes dead-code candidates. LSP may disprove a candidate by finding a
use. An empty or incomplete LSP result does not prove deadness.

## Result Semantics

One numeric confidence value is insufficient. Normalized facts carry:

- provider and exact version;
- repository, workspace, source, and document identity;
- requested and effective capability;
- negotiated position encoding;
- exact ranges;
- freshness and readiness;
- provider-reported, structural, ambiguous, unresolved, unsupported, partial, or
  timed-out state;
- provenance for every merged observation.

Common resolution labels are `lsp_confirmed`, `graph_confirmed`, `lsp_and_graph`,
`lsp_only`, `graph_candidate`, `ambiguous`, `unresolved`, and `unsupported`.

## Token Contract

Raw LSP JSON is never sent to an agent. Exact small results use a deterministic
renderer rather than being forced through the corpus-oriented Context Compiler.
The renderer returns:

- status, freshness, provider, symbol, and total count first;
- groups by path and containing symbol;
- at most 10 locations by default;
- signatures without bodies;
- explicit truncation plus the next stateless `offset`;
- source bodies or full files only after an explicit expansion request;
- stable ordering and no silent clipping.

The Context Compiler remains responsible for architecture, impact, grounded QA,
and other broad responses that combine code, knowledge, decisions, tests, and
multiple evidence spans.

## Cache Policy

The first production slice adds no semantic result cache. The warm language server
already owns its incremental semantic state. LLM Wiki may coalesce identical
concurrent requests and reuse a normalized fact within one compound operation.

A session fact cache may be proposed later only after measurements show material
end-to-end benefit. It must cache normalized facts, not formatted agent responses,
and must be keyed by workspace epoch, server version, configuration, document
versions, method, and parameters.

## Python First

The first production language is Python through a pinned Pyright language server.
The Python slice includes:

- discovery and diagnosis when Pyright is absent;
- lazy startup and bounded readiness;
- definitions, references, hover/type information, document/workspace symbols,
  diagnostics, and call hierarchy only when advertised;
- capability-honest behavior for unsupported implementations;
- synchronization after create, edit, rename, and delete;
- Windows, Linux, and macOS verification;
- compact MCP responses and structural fallback.

Language-server installation is a separate explicit operator action. Query paths
never download or silently update a server.

The qualified profile pins Pyright `1.1.411` and records the exact executable,
package digest, Node major version, initialization options, and configuration
fingerprint. Discovery prefers a matching project-local installation, then an
explicitly managed LLM Wiki installation, then a matching system installation.
Version or digest mismatch is `degraded` and is reported by doctor; it is never
silently treated as the qualified profile. Exact packaging and tested Node versions
belong to the implementation plan and lockfiles, not a floating `latest` command.

The user-approved runtime paths are
`cache/code-tools/pyright/1.1.411/` for the regenerable managed installation and
`run/lsp/<owner-nonce>/` for process-owned temporary files, cancellation markers,
and cleanup evidence. The latter follows the existing `run/` deletion contract and
must not outlive an active owner except as bounded failure evidence reported by
doctor. Evidence and lease publication serialize one hidden temporary name per
owner. Windows reserves that intent before the create call because its wrapper can
create the entry and then fail identity validation. POSIX records it immediately
after atomic creation returns and before validation. If later validation or
handle-relative deletion fails, that exact name remains owned and blocks new
publication, evidence success, lease removal, scratch deletion, and owner close. The
lifecycle remains `CLEANUP_PENDING` until retry deletes the name or proves it absent.
Successful terminal layouts contain no hidden temporary files.

Other language families are future candidates, not part of this approved target.
Each requires separate capability, installation, execution-risk, platform, and
benchmark qualification after Python passes its release gates.

## Security

The Python slice runs a pinned Pyright subprocess with the current user's OS
permissions and is therefore limited to operator-trusted local repositories.
“Read-only” means the LSP client exposes no mutation method, rejects arbitrary
server commands, and never intentionally writes project files; it is not an OS
sandbox and does not claim that Pyright cannot read or write other user-accessible
paths. The client validates every requested and returned source path against the
repository, bounds time and process count, passes an explicit minimal environment
allowlist, redacts logs, and does not place agent, cloud, SSH, or package-registry
credentials in that environment. Pyright may still read explicitly configured
external environments, stubs, and library code; these inputs are part of the
configuration fingerprint and provenance.

Process containment is platform-qualified. A Windows Job Object owns the assigned
server and its child processes. On Linux and macOS, a POSIX process group owns the
pinned Pyright server and descendants only while they remain in that group. A
hostile descendant can call `setsid()` and leave that group; portable containment
of that escape is unsupported. The POSIX contract therefore applies to the pinned,
qualified Pyright profile in trusted repositories, not to arbitrary hostile
executables. The runtime does not use a racy process-ancestry scan to imply stronger
ownership.

Higher-risk servers that may evaluate builds, plugins, macros, or generators need
a separately approved trust policy before implementation. The completed sealed
workspace utilities from old Task 5 remain available for future one-shot or
untrusted analyzers but are not in the live point-query hot path.

## Reliability Gates

Python is not production-ready until tests prove:

- no stale answer in the deterministic edit/rename/delete suite;
- no orphan process inside the platform-qualified ownership boundary after normal
  shutdown, crash, timeout, or cancellation;
- successful bounded recovery after forced server failure;
- correct Unicode and Windows URI behavior;
- explicit degraded states for missing dependencies, broken projects, unsupported
  capabilities, and incomplete indexing;
- stable structural fallback when LSP is absent or failed;
- no automatic source, knowledge, graph, Git, package, or environment mutation.

Protocol and fixture tests run on Windows, Linux, and macOS. Real Pyright
integration and process-tree tests run on all three supported OS families with the
pinned profile before release.

## Measurement Gates

The implementation includes a reproducible Python benchmark on fixed synthetic
fixtures and an operator-supplied local project corpus. It records:

- definition exact-location accuracy;
- references, calls, and impact precision/recall/F1 where gold evidence exists;
- task success and citation correctness;
- uncached, cache-read, output, and raw tool tokens per solved task;
- cold startup, warm p50/p95 latency, edit-to-fresh latency, peak RSS, and errors;
- failure recovery, stale-result, and orphan-process rates.

Initial acceptance requires at least 99% exact definitions, at least 95% reference
F1 on qualified fixtures, zero stale fixture answers, zero orphan processes inside
the platform-qualified ownership boundary, and 100% recovery in the bounded crash
suite. The default response is at most 10 items and 1,200 estimated tokens. On the
fixed 100 KLOC Python qualification repository,
warm engine overhead above a direct warmed Pyright request must be no more than
20 ms at p95, cold readiness must complete within 60 seconds, and LLM Wiki process
RSS overhead excluding Pyright must stay below 100 MiB. The benchmark manifest pins
repository commit, Python, Pyright, Node, OS, hardware class, and gold queries. An
operator-supplied private corpus is an additional qualification set, not a
replacement for the fixed public fixture. Market superiority remains unclaimed
until paired tests against pinned releases of competing systems pass predefined
quality, latency, token, and reliability thresholds.

## Effect On The Previous Plan

Tasks 1-5 of `2026-07-21-code-kernel-foundation-python.md` remain completed and
useful. The normalized capability/position vocabulary, Graph v3 compatibility,
fixtures, and workspace safety library are retained.

Tasks 6-16 are replaced. The new plan removes exact-invocation consent databases,
sealed one-shot execution from the interactive hot path, mandatory SCIP import,
the custom CPython semantic analyzer, LSP publication into Graph v3, and
capture-to-publication orchestration. It replaces them with the owned LSP runtime,
Pyright profile, code-navigation facade, compact renderer, existing-MCP modes,
doctor/install diagnostics, reliability tests, local-project qualification, and
documentation.

## Sources

- LSP 3.18: https://microsoft.github.io/language-server-protocol/specifications/lsp/3.18/specification/
- Pyright: https://github.com/microsoft/pyright
- Serena v1.6.1 and SolidLSP reference behavior: https://github.com/oraios/serena/releases/tag/v1.6.1
- Microsoft multilspy: https://github.com/microsoft/multilspy
- SCIP v0.9, retained as a future batch option rather than the interactive path: https://github.com/scip-code/scip/releases/tag/v0.9.0
- clangd indexing model: https://clangd.llvm.org/design/indexing
- Existing structural target and completed foundation: `docs/superpowers/plans/2026-07-21-code-kernel-foundation-python.md`, Tasks 1-5.
