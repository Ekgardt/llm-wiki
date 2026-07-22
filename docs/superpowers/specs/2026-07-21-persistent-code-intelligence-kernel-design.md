# Persistent Code-Intelligence Kernel Design

> **Superseded after foundation Tasks 1-5:** The one-shot consent/SCIP/publication
> direction in this design was replaced on 2026-07-22 by
> `docs/superpowers/specs/2026-07-22-read-only-lsp-navigation-design.md`. This file
> remains historical design evidence.

**Status:** Superseded after foundation Tasks 1-5 on 2026-07-22.

**Product phase:** Phase 2 of
`docs/superpowers/plans/2026-07-19-solo-operator-superset-roadmap.md`.

## Summary

LLM Wiki will maintain one local, persistent map of a repository so every agent
can reuse precise code relationships instead of repeatedly scanning files. The
map combines compiler or language-server facts, resilient syntax extraction,
and exact text search without presenting lower-confidence results as precise.

The first complete Phase 2 release covers at least eleven named programming
languages: Python, JavaScript, TypeScript, Java, C#, Go, Rust, C, C++, PHP, and
Ruby. SQL, Bash, JSON, YAML, TOML, Docker, Kubernetes, and Terraform/HCL are
first-class repository artifacts, but they are not marketed as having the same
compiler-level guarantees unless an analyzer proves them.

No persistent language-server daemon is required. Precise analyzers run as
bounded local jobs, emit SCIP or a validated adapter payload, and exit. LLM Wiki
normalizes their facts into the existing immutable repository generation.

## User Problem

Agents currently spend context and time rediscovering basic repository facts.
They can miss cross-file relationships, confuse similarly named symbols, infer
links that do not exist, and repeat the same exploration in every session.

The kernel must support this workflow:

1. The operator asks an agent to change behavior, investigate an incident, or
   assess impact.
2. LLM Wiki binds the request to the exact repository, checkout, worktree, and
   generation.
3. It finds the relevant definition, references, callers, imports, tests,
   routes, database objects, and deployment configuration.
4. It labels every result by evidence quality and analysis coverage.
5. The Context Compiler packs only the most useful verified evidence.
6. After an acknowledged source change, affected code facts are invalidated and
   rebuilt before they can be returned as current.
7. Later agents reuse the same local map without running a language server for
   every query.

## Goals

- Replace ordinary codebase-memory-mcp navigation and impact workflows without
  a separate installation.
- Provide exact identifier, definition, reference, import, call, type,
  inheritance, implementation, route, schema, and deployment evidence where a
  suitable analyzer can prove it.
- Remain useful when a project is incomplete, dependencies are missing, or a
  precise analyzer is unavailable.
- Make unsupported, partial, ambiguous, stale, and failed analysis explicit.
- Start and query quickly from an existing generation with no daemon.
- Preserve repository, source hash, analyzer, build environment, and exact span
  provenance for every durable fact.
- Reduce context tokens by at least 50% versus raw-file exploration at
  non-inferior task success.

## Non-Goals

- Replacing compilers, IDE refactoring engines, or build systems.
- Claiming that static analysis proves all behavior in dynamic languages.
- Treating an absent edge as proof that no edge exists.
- Requiring every language tool in the base Python installation.
- Uploading source code or analyzer payloads to a remote service.
- Keeping language servers alive between indexing jobs.
- Activating partial repository generations as complete generations.
- Adding a second graph, active pointer, or runtime root.

## Approaches Considered

### Broad syntax-only support

This approach would advertise many Tree-sitter grammars and infer most links
from names and syntax. It is easy to package, but it cannot reliably distinguish
same-named symbols, build variants, interfaces, generated code, or resolved
imports. It is rejected as the primary architecture because breadth without
truthful precision does not satisfy the product goal.

### Always-running language servers

This approach would keep one server alive for each language and ask it questions
on demand. It can be precise, but startup, memory, lifecycle, environment drift,
and platform setup become permanent operational burdens. LSP is an interactive
query protocol rather than a complete persistent-index contract. It is rejected
as the runtime architecture.

### Layered persistent kernel

This is the selected approach. Compiler-backed or semantic analyzers produce
precise facts in bounded one-shot jobs. Tree-sitter supplies resilient structure.
Exact and lexical search supplies universal fallback. Every layer retains its
own provenance, and a deterministic projection selects the strongest usable
fact for each query.

## Architecture

### Existing generation layout remains the publication boundary

The kernel introduces `evidence-graph/v3` inside the existing
`corpus-generation/v2` directory and catalog layout. It does not introduce
another active catalog, graph database, daemon, or runtime root. Markdown, Git,
project journals, and captured source bytes remain authoritative; every code
index is disposable derived state.

Graph v3 is a new exact schema, not an in-place mutation of graph v2. It retains
the v2 source, node, occurrence, assertion, evidence, observation, and dependency
contracts and adds normalized analyzer-run, coverage, symbol-claim, diagnostic,
slice-activation, and validity tables. The graph schema identifier, SQLite
`user_version`, exact table/index contract, extractor identity, and generation
manifest all advance together.

Readers remain able to open graph v2 generations for their existing structural
capabilities. They expose no precise analyzer capability and never synthesize v3
coverage. New builders emit graph v3 only after its complete schema validates.
Catalog fallback may select v2 or v3 only when repository scope and the requesting
operation's required capability match. Incremental reuse crosses the schema
boundary only through captured authoritative source bytes; v2 graph rows are not
copied into v3. Downgrade keeps v2 readable but cannot reinterpret a v3 database.
Because caches are derived, installed vaults migrate by building and atomically
activating a new v3 generation while retaining the previous v2 generation and all
legacy caches for rollback.

`scripts/code_index.py` will be the internal read/query facade. It will query the
active repository-scoped Evidence generation and return bounded results with
source spans, analyzer provenance, capability, coverage, and freshness. Existing
MCP tools may use this facade, but Phase 2 does not add tools merely to expose
internal tables.

### Four evidence levels

1. **Compiler:** Facts emitted from a compiler or compiler-grade semantic model.
2. **Semantic engine:** Facts resolved by a language server or type-analysis
   engine but not guaranteed to represent a complete compiler build.
3. **Syntax:** Facts extracted from parsed source structure without cross-file
   semantic proof.
4. **Lexical:** Exact text, path, and token matches.

Higher levels may override a conflicting preferred result, but lower-level facts
remain queryable with their provenance. Syntax can supplement outlines or spans
that a precise analyzer omits. Lexical evidence never overrides semantic
navigation.

### Analyzer run and coverage

Every analyzer invocation creates an immutable run record, including failures.
It is bound to:

- repository, checkout, worktree, and source generation;
- exact analyzer and adapter version or executable digest;
- protocol and protocol version;
- sanitized invocation hash;
- source, manifest, lockfile, build configuration, SDK, target, and feature
  hashes that affect semantic resolution;
- declared capabilities;
- actual coverage for each document and capability;
- outcome: complete, partial, failed, cancelled, rejected, or superseded.

Coverage is recorded independently for definitions, references, types,
implementations, calls, diagnostics, and other capabilities. A completed process
does not imply complete reference coverage.

The coverage unit is machine-checkable:

```text
(repository scope, project snapshot, analyzer, build target, build configuration,
 document revision, capability)
```

The expected document set comes from the canonical source manifest plus the
analyzer's declared build/source roots. Expected build targets and configurations
come from captured build manifests and the explicit indexing request. Generated
files, excluded roots, unresolved external dependencies, unsupported targets,
parse failures, and analyzer omissions are terminal coverage records rather than
silent absences.

A scope is complete for a capability only when every expected unit has a terminal
`complete`, `unsupported`, or explicitly excluded result and the query's closed
scope excludes unsupported units. Project-wide negative answers additionally
require all applicable build targets and configurations, generated-source
availability, dependency resolution, and analyzer support for that negative fact.
Overloads, dynamic dispatch, reflection, generated calls, and unavailable external
dependencies keep the corresponding negative claim open-world.

### Normalized fact model

The normalized boundary stores immutable claims before producing a preferred
graph. The minimum entities are:

- analysis run, capability, coverage, and active analysis slice;
- project snapshot, document, document revision, and source hash;
- symbol identity and symbol claims;
- occurrence, role, and exact source range;
- typed relationship;
- diagnostic and related location;
- validity and stale reason.

SCIP global symbol strings remain exact identities. SCIP local symbols are
scoped to analyzer run and document revision. LSP monikers are used only within
their declared uniqueness scope. LSP symbols without monikers receive
revision-scoped synthetic identities and are never treated as stable global
symbols.

All ranges are normalized to zero-based, half-open UTF-8 byte coordinates and
validated against the exact captured source bytes. Invalid, stale, clamped, or
out-of-bounds analyzer ranges are rejected.

### Atomic slice activation

Facts are inserted as inactive immutable claims. Successfully completed slices
are selected in one generation build. Failed, skipped, or absent slices do not
delete prior valid evidence silently. A successful empty slice replaces prior
facts only when coverage explicitly says the relevant scope is complete.

Any source hash, build environment, analyzer semantics, position encoding, or
dependency change that invalidates a slice makes it unusable for normal queries.
Soft-stale facts may be returned only with a stale marker when the query contract
allows it. Hard-stale facts never answer normal queries.

## Language Matrix

| Language | Primary precise analyzer | Immutable revision | Required fallback | Qualification limit |
|---|---|---|---|---|
| Python | `scip-python` / Pyright | `0.6.6`, MIT | CPython `ast` + `symtable`, then Tree-sitter | Dynamic imports, reflection, monkey patching |
| JavaScript | `scip-typescript` inferred JS mode | `0.4.0`, Apache-2.0 | TypeScript compiler helper, then Tree-sitter | Missing types reduce precision |
| TypeScript | `scip-typescript` | `0.4.0`, Apache-2.0 | TypeScript compiler helper, then Tree-sitter | Project/workspace configuration is identity-bearing |
| Java | `scip-java` | `0.13.1`, Apache-2.0 | Ephemeral Eclipse JDT LS, then Tree-sitter | Supported JDK and truthful build import required |
| C# | `scip-dotnet` | `0.2.14`, Apache-2.0 | One-shot Roslyn/MSBuild adapter, then Tree-sitter | Restore, SDK, generated sources, workloads |
| Go | `scip-go` | `0.2.7`, Apache-2.0 | Ephemeral `gopls`, then Tree-sitter | Module/build tags and dependency cache matter |
| Rust | `rust-analyzer scip` | `cac0779549328e4bd4b808000c03307f1721f869`, MIT OR Apache-2.0 | Ephemeral rust-analyzer LSP, then Tree-sitter | Macros, targets, known relationship gaps |
| C | `scip-clang` | `0.4.0`, Apache-2.0 | `clangd-indexer` or ephemeral clangd, then Tree-sitter | Complete compilation database required |
| C++ | `scip-clang` | `0.4.0`, Apache-2.0 | `clangd-indexer` or ephemeral clangd, then Tree-sitter | Build variants, generated headers, compiler flags |
| PHP | `scip-php` | `71a5b117ec4c5dd2af302e363410e604e5df309e`, MIT | Ephemeral Phpactor, then Tree-sitter | Experimental; cannot ship as precise until qualification passes |
| Ruby | `scip-ruby` / Sorbet | `0.4.7`, Apache-2.0 | Ephemeral Sorbet LSP, then Tree-sitter | Precision depends on Sorbet typing coverage |

The exact pinned analyzer version can advance only through measured compatibility
evidence. Unsupported platforms degrade explicitly. In particular, published
`scip-clang` and `scip-ruby` binaries do not currently provide complete Windows
coverage, so Windows uses the declared fallback until signed native packaging is
qualified.

Support is recorded per capability, not per language name. Every qualified
analyzer/platform/build combination publishes a matrix for definitions,
references, calls, types, inheritance, implementations, diagnostics, incremental
replacement, and assumption-bound negative answers. Each cell is one of
`compiler`, `semantic`, `syntax`, `lexical`, `unsupported`, or `unqualified`, with
actual completed coverage. An analyzer's brand never upgrades a cell without
validated run evidence.

## Repository Topology

Programming-language facts are connected to structural repository artifacts:

- Python and JavaScript package manifests and lockfiles;
- SQL tables, migrations, and embedded query evidence;
- HTTP route and handler declarations;
- Dockerfiles and Compose services;
- Kubernetes workloads, services, and configuration;
- Kustomize overlays and rendered-resource provenance;
- Terraform modules, resources, and references;
- CI workflows, jobs, artifacts, and deployment triggers;
- explicit package/workspace manifests, exports, and lockfile dependencies;
- test-to-production-code relationships;
- generated, vendored, ignored, and unavailable source classifications.

Initial framework extractors target FastAPI, Flask, Django, Express, NestJS, and
Next.js. Framework edges are labeled structural or inferred unless a semantic
analyzer directly proves the relationship.

## Query Behavior

The kernel supports bounded operations for:

- exact symbol and path lookup;
- definitions and declarations;
- references and callers;
- implementations, overrides, inheritance, and type definitions;
- imports, exports, packages, and dependency paths;
- route, schema, service, and deployment relationships;
- change impact and stale-evidence explanation;
- token-budgeted repository maps;
- explicit capability, coverage, and analyzer-health reporting.

Results always identify repository scope, source generation, source hash, exact
span, evidence level, analyzer, coverage status, and ambiguity. A negative answer
is allowed only under explicitly recorded closed-world assumptions and complete
coverage for the requested scope.

## Failure And Degraded Behavior

- Missing analyzer: use the strongest available lower level and report the
  missing capability.
- Build import failure: retain syntax and lexical coverage; do not activate a
  partial precise slice as complete.
- Analyzer timeout or cancellation: preserve completed slices only when their
  coverage is independently terminal and validated.
- Source change during analysis: reject the stale slice. Facts for the old source
  remain historical evidence only and cannot answer a current-worktree query.
- Precise rebuild failure after a source change: a new generation may publish
  current syntax and lexical evidence, but it cannot carry old-source precise
  slices forward as current. The response reports precise analysis unavailable
  for the changed scope.
- Corrupt payload or invalid range: reject the affected run or slice and retain
  the previous valid generation.
- Ambiguous symbol: return bounded candidates with evidence; never choose a
  winner silently.
- Unsupported language: exact and lexical search remain available, marked as
  lexical rather than precise navigation.
- Network unavailable: indexing and querying still operate from installed tools
  and local dependency caches. Runtime downloads are never implicit.

## Security And Privacy

- Syntax and lexical extraction are the default for an untrusted repository.
  Build-aware precise analysis requires explicit operator approval for the
  repository, analyzer family, and captured invocation class. Approval is
  revocable and does not transfer automatically to another repository.
- Package restoration, build generation, and analyzer execution are separate
  actions. LLM Wiki never runs package scripts, Maven/Gradle plugins, MSBuild
  targets, compiler plugins, code generators, or repository commands merely
  because a query was made.
- A precise job receives a sanitized environment allowlist with no inherited
  credentials, agent tokens, SSH configuration, cloud configuration, or user
  home. It uses a per-run scratch home/cache/output under the existing runtime
  `run/` area and a captured source workspace that is read-only where the host
  platform can enforce it. Analyzer output is the only expected writable product.
- Analyzer subprocesses receive bounded time, memory where enforceable, output,
  child count, and complete process-tree cleanup.
- Network is denied when the platform's qualified isolation mechanism can enforce
  it. If a platform cannot enforce the declared filesystem/process/network
  boundary, build-aware execution is labeled `trusted-execution-required` and is
  disabled until the operator explicitly accepts that weaker boundary.
- Windows, Linux, and macOS isolation are qualified separately. No portable
  subprocess wrapper is described as a security sandbox.
- Indexing is local and runs after separately approved dependency restoration.
- Source text is not uploaded to Sourcegraph or another service.
- Raw analyzer arguments are hashed and redacted before persistence.
- Analyzer diagnostics and failures pass through existing secret and path
  redaction.
- Generated paths are normalized beneath the captured repository root; links,
  reparse escapes, absolute paths, traversal, and case collisions fail closed.
- Analyzer binaries, packages, licenses, hashes, and SBOM entries are recorded in
  the eventual signed component package.

## Performance And Quality Gates

Reference measurements use a declared local machine with eight physical CPU
cores, 32 GB RAM, and NVMe storage. Cold and warm states, dependency-cache state,
language, analyzer, build import, target count, configuration count, and coverage
are reported separately.

- Existing-index MCP readiness: p95 below 300 ms.
- First exact, path, or symbol result during cold indexing: below 1 second.
- Warm exact/lexical/symbol query: p95 below 100 ms.
- Warm bounded graph query: p95 below 200 ms.
- One-file syntax refresh: p95 below 500 ms; precise refresh is reported per
  analyzer and must not regress its pinned comparator.
- One hundred changed files syntax refresh: p95 below 5 seconds.
- Cold 100 KLOC syntax/lexical repository map: p95 below 15 seconds.
- Cold 1 MLOC syntax/lexical repository map: below 120 seconds.
- Precise full analysis has a measured per-language budget and must be no slower
  than its pinned local comparator at equal build scope; a universal compiler
  completion time is not claimed.
- Exact-definition Top-1 on statically resolvable cases: at least 95% per claimed
  precise language.
- Cross-file any-gold Recall@5: at least 80% per claimed language.
- Citation resolution and source-hash validity: 100%.
- Evidence precision lower 95% confidence bound: at least 97%.
- Supported-negative precision lower 95% confidence bound: at least 99%.
- Wrong-repository and wrong-generation answers: 0.
- Clean-rebuild source inventory parity: 100%.
- Incremental query parity: at least 99.5%, with every difference explained.
- Invented path or symbol rate: below 0.5%.
- Stale-result rate after acknowledged update: 0%.
- At least 50% fewer context tokens than raw-file exploration at non-inferior
  fixed-agent task success.
- Warm latency is no worse than the pinned codebase-memory-mcp comparator at equal
  repository scope.

Phase 2 is not complete from aggregate scores alone. Every marketed precise
language must independently meet its floor or be documented at a lower
capability level.

## Verification Strategy

The evidence program includes:

- RepoBench-R for Python and Java cross-file retrieval;
- CrossCodeEval for Python, Java, TypeScript, and C#;
- CodeRAG-Bench repository retrieval and oracle-gap arms;
- SWE-bench Verified localization for Python;
- SWE-bench Multilingual and Multi-SWE-bench for JavaScript/TypeScript, Java, Go,
  Rust, C, C++, PHP, and Ruby where represented;
- a frozen private navigation set with definitions, references, implementations,
  imports, tests, configuration, absent answers, stale indexes, and ambiguous
  symbols;
- a published navigation-qrel set with at least 500 stratified queries per
  marketed precise language, at least 20 repositories per language where
  available, compiler/language-server seed labels, manual adjudication, and
  explicit absent-answer cases;
- paired codebase-memory-mcp, raw filesystem, exact search, FTS, and ablation
  runs on identical commits and token budgets;
- clean/incremental parity, crash injection, process cleanup, offline operation,
  wrong-repository, wrong-generation, and source-race tests.

Public superiority claims require a positive paired lower 95% confidence bound
and immutable raw result ledgers. A smoke fixture, one language, or a fake adapter
does not complete Phase 2.

## Delivery Order

1. Graph v3 schema, normalized analyzer-run/capability/coverage contracts, SCIP
   parser, isolation preflight, and v2 reader compatibility.
2. A complete Python vertical slice: precise and syntax ingestion, atomic
   activation, hard-stale invalidation, reconciliation, store-first queries,
   repository map, degraded behavior, and benchmark evidence.
3. A complete JavaScript/TypeScript vertical slice with the same freshness and
   query contract.
4. Java and C# vertical slices.
5. Go and Rust vertical slices.
6. C/C++ vertical slices and Windows clangd fallback.
7. PHP and Ruby precise/partial slices with explicit maturity labels.
8. SQL, package, route, Docker/Compose, Kubernetes/Kustomize, Terraform, and CI
   topology integrated into the same query facade.
9. Cross-language impact, existing MCP integration, complete clean/incremental
   parity, and optional bounded watcher plus mandatory reconciliation.
10. Signed analyzer packaging, immutable artifact manifests, SBOM, full
    per-language qualification, and competitive evidence.

Each delivery is test-first and may be committed independently. The product does
not advertise the Phase 2 replacement claim until every exit gate passes.

## Current-Practice Sources

Verified on 2026-07-21. Versions and revisions in the language matrix are part
of the qualification input; packaged artifacts additionally require SHA-256 and
target-platform records before release.

- SCIP design and schema: <https://github.com/scip-code/scip>
- Sourcegraph precise and syntactic navigation:
  <https://sourcegraph.com/docs/code-navigation/precise-code-navigation>
- Sourcegraph indexer guidance:
  <https://sourcegraph.com/docs/code-navigation/writing-an-indexer.md>
- LSP 3.18: <https://microsoft.github.io/language-server-protocol/specifications/lsp/3.18/specification/>
- Tree-sitter: <https://tree-sitter.github.io/tree-sitter/>
- Pyright internals: <https://github.com/microsoft/pyright/blob/main/docs/internals.md>
- TypeScript Language Service API:
  <https://github.com/microsoft/TypeScript/wiki/Using-the-Language-Service-API>
- rust-analyzer architecture:
  <https://rust-analyzer.github.io/book/contributing/architecture.html>
- clangd indexing: <https://clangd.llvm.org/design/indexing>
- Python SCIP indexer: <https://github.com/sourcegraph/scip-python>
- JavaScript/TypeScript SCIP indexer:
  <https://github.com/sourcegraph/scip-typescript>
- Java SCIP indexer: <https://github.com/scip-code/scip-java>
- C# SCIP indexer: <https://github.com/sourcegraph/scip-dotnet>
- Go SCIP indexer: <https://github.com/scip-code/scip-go>
- Rust SCIP implementation:
  <https://github.com/rust-lang/rust-analyzer/blob/master/crates/rust-analyzer/src/cli/scip.rs>
- C/C++ SCIP indexer: <https://github.com/sourcegraph/scip-clang>
- PHP SCIP indexer at pinned source revision:
  <https://github.com/davidrjenni/scip-php/commit/71a5b117ec4c5dd2af302e363410e604e5df309e>
- Ruby SCIP indexer: <https://github.com/sourcegraph/scip-ruby>
- Phpactor local language server:
  <https://phpactor.readthedocs.io/en/master/usage/standalone.html>
- Sorbet language server: <https://sorbet.org/docs/lsp>
- RepoBench: <https://github.com/Leolty/repobench>
- CrossCodeEval: <https://github.com/amazon-science/cceval>
- CodeRAG-Bench: <https://github.com/code-rag-bench/code-rag-bench>
- Multi-SWE-bench: <https://github.com/multi-swe-bench/multi-swe-bench>
