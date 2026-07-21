# Solo-Operator Superset Product Roadmap

> **For agentic workers:** This is the canonical product roadmap. Each phase has
> a separate implementation plan and must use research-before-implementation,
> test-driven-development, and two-stage review. Completing one phase is not
> completion of the product.

**Goal:** Build one local-first product that gives a single operator reliable
memory, code intelligence, task control, and minimal evidence-backed context
across many agents and sessions, making separate Graphify and
codebase-memory-mcp installations unnecessary.

**Architecture:** Markdown, Git, raw episodes, project journals, accepted
decisions, and accepted artifacts remain authoritative. Immutable
repository-scoped generations provide derived search, vectors, and graph;
operational SQLite coordinates work; one Adaptive Context Compiler serves every
agent entry point. A durable task control plane survives individual sessions and
exposes only exceptions and consequential decisions to the operator.

**Tech Stack:** Python 3.10+, SQLite rollback journal with `synchronous=FULL`,
FTS5, NumPy exact search, optional USearch/LanceDB, Tree-sitter, SCIP/LSP/Jedi,
Markdown/OKF, MCP stdio and optional Streamable HTTP, pytest, Ruff, Git, optional
native code-index component, local web console.

---

## Authority

This roadmap supersedes the 2026-07-16 plan only as the final product scope. The
older plan remains the historical implementation record for Evidence Graph and
Adaptive Context Compiler Tasks 1-30.

The immutable product decision is:
`knowledge/notes/solo-operator-superset-product-decision.md`.

The dated technical research is:
`docs/superpowers/specs/2026-07-19-world-class-practices-matrix.md`.

No agent may remove or defer a requirement from this roadmap without:

1. describing the user impact;
2. obtaining explicit user approval;
3. recording a superseding decision;
4. updating this roadmap and `docs/STRUCTURE.md`.

## North star and hard gates

Primary outcome:

```text
accepted verified outcomes
---------------------------------------------
million lifecycle tokens + operator hour
```

Hard gates are not averaged into a headline score:

- citation resolution and hash validity: 100%;
- evidence precision lower 95% bound: at least 97%;
- supported-negative precision lower 95% bound: at least 99%;
- wrong-repository and wrong-generation answers: 0;
- cross-project/private-scope leakage: 0;
- acknowledged authoritative writes lost: 0 in 3,000 release fault schedules;
- clean-rebuild source inventory parity: 100%;
- incremental query parity: at least 99.5%, with every difference explained;
- false automatic supersession upper 95% bound: at most 1%;
- public superiority only when the paired lower confidence bound is positive.

## Dependency order

```text
0. Freeze contract and research
  -> 1. Production truth and generation correctness
  -> 2. Persistent code-intelligence kernel
  -> 3. Multi-repository evidence and contracts
  -> 4. Temporal four-layer memory
  -> 5. Adaptive context everywhere
  -> 6. Solo-operator task control plane
  -> 7. Interfaces, broad ingestion, and packaging
  -> 8. Competitive proof and release
```

Phases 4 and 6 can be researched in parallel with Phases 2-3, but production
implementation follows the dependency order whenever schemas or state are shared.

## Phase 0: Contract and research

**User problem:** Product intent repeatedly weakened as component plans were
treated as the final goal.

**Solution:** Keep one immutable public product decision, one current-practice
matrix, one master roadmap, and one explicit competitive scorecard.

**Deliverables:**

- public decision page;
- updated agent contract and structure reference;
- best-practice matrix dated 2026-07-19;
- this roadmap;
- subsystem plan template requiring use case, evidence, TDD, production path,
  migration, rollback, and benchmark;
- machine-readable roadmap requirements checked by documentation tests.

**Exit gate:** Every approved capability maps to a phase, a user workflow, a
research source, an implementation plan, and a measurable acceptance gate.

## Phase 1: Production truth and generation correctness

**User problem:** A code query can read a generation for the wrong repository,
and routine maintenance can activate an evidence-only generation that retrieval
cannot consume.

**Solution:** Make repository identity and a complete artifact contract mandatory
at build, activation, and read time.

**Scope:**

- repository-scoped request and generation identity;
- canonical repository/checkout/worktree manifest;
- complete required artifact contract;
- FTS+graph publication from the same captured source snapshot;
- code-language classification repair;
- workspace-wide extraction rather than isolated per-file resolution;
- grounded QA through MCP;
- SessionStart through Context Compiler;
- reliable installer exit-code checks;
- explicit compatibility fallback only.

**Detailed plan:**
`docs/superpowers/plans/2026-07-19-production-truth-foundation.md`.

**Exit gate:** No wrong repository result, no incomplete active generation, and
full production-path tests pass from clean install through maintenance and query.

## Phase 2: Persistent code-intelligence kernel

**User problem:** Agents repeatedly scan source files and still produce incomplete
or fabricated cross-file relationships.

**Solution:** Use a deterministic persistent code index with compiler-backed
semantic enrichment and explicit unresolved coverage.

**Scope:**

- internal native-kernel boundary and license/attribution decision;
- incremental Tree-sitter extraction;
- symbol, definition, reference, import, call, type, and inheritance indexes;
- SCIP/LSP/Jedi enrichment with declared capability;
- lexical code search and exact snippets;
- bounded read-only graph query;
- framework and IaC extractors;
- code-specific coverage and negative-claim contract;
- capability-based language matrix;
- optional bounded watcher and mandatory reconciliation;
- signed component packaging and SBOM.

**Exit gate:** Paired code tasks have non-inferior success, at least 50% fewer
context tokens than raw-file exploration, no unsafe negative claims, and no worse
warm query latency than the pinned codebase-memory-mcp comparator at equal scope.

## Phase 3: Multi-repository evidence and contracts

**User problem:** One operator manages many repositories, while current persistent
evidence is vault-specific and cannot reliably answer cross-service impact.

**Solution:** Build one portfolio generation from explicitly registered repository
snapshots and evidence-backed contracts.

**Scope:**

- workspace registry projected from project journals;
- stable repository and checkout identities;
- branch/worktree isolation;
- portfolio generation manifests;
- package/service/API/schema/event/deployment relations;
- shared dependency deduplication;
- cross-repository path, architecture, and impact queries;
- selective access and project isolation;
- repository add/remove/rename and unavailable-source handling.

**Exit gate:** At least 90% recall for labeled cross-repository contracts, zero
scope leakage, and exact incremental/clean portfolio inventory parity.

## Phase 4: Temporal four-layer memory

**User problem:** Facts, experiences, procedures, and future commitments currently
share incomplete lifecycle semantics, allowing stale plans and weak inferences to
be mistaken for current truth.

**Solution:** Separate memory products and add bi-temporal claim provenance.

**Scope:**

- immutable episodic source records;
- semantic claims with valid and transaction time;
- outcome-backed procedural promotion;
- prospective triggers, assumptions, expiry, cancellation, and activation;
- create/corroborate/refine/supersede/quarantine proposals;
- preference scope and revocation;
- stale-on-change and source revalidation;
- evidence-preserving forgetting and archive;
- point-in-time and current-state query semantics.

**Exit gate:** Provenance 100%, current-state accuracy at least 95%, historical
accuracy at least 90%, false supersession at most 1%, and zero invalid
prospective activations with stale authorization.

## Phase 5: Adaptive context everywhere

**User problem:** SessionStart and several code paths bypass the strongest context
planning and grounding logic, causing inconsistent quality and wasted tokens.

**Solution:** One request/plan/materialize/pack/verify pipeline for all consumers.

**Scope:**

- common context request contract;
- exact, lexical, vector, graph, temporal, Git, project, and no-context routes;
- task-aware L0/L1/L2 materialization;
- complete-item budget packing;
- conditional reranking;
- stable provider cache blocks;
- source and representation deduplication;
- grounded answer MCP operation;
- SessionStart, handoff, impact, task, and QA migration;
- retrieval outcome feedback without raw prompt retention.

**Exit gate:** SessionStart at most 2,000 tokens, ordinary retrieval at most 4,000,
deep retrieval at most 8,000, and at least 90% context reduction against full
history on memory tasks at non-inferior answer quality.

## Phase 6: Solo-operator task control plane

**User problem:** Sessions are temporary, but the operator currently coordinates
assignments, handoffs, decisions, conflicts, budgets, and completion manually.

**Solution:** Make task commitments and accepted artifacts durable across agents
and sessions.

**Scope:**

- Project, Objective, Task, Execution, Workspace, Artifact, Decision, Checkpoint,
  Budget, Capability, Exception, and Event contracts;
- append-only task/project events and deterministic projections;
- assignment preflight and acknowledgement;
- leases, fencing, heartbeat, pause, cancel, replace, and recovery;
- artifact publication, verification, acceptance, and rejection;
- delegation fan-out/depth/budget limits;
- semantic overlap and workspace conflict detection;
- selective decision propagation;
- structured handoff and ownership transfer;
- hierarchical budgets and convergence detection;
- least-privilege capability envelopes;
- prospective activation;
- portfolio brief and exception grouping.

**Exit gate:** Lost acknowledged work 0, duplicate irreversible effects 0, silent
conflicts 0, accepted artifacts without verification 0, and operator
re-explanation burden improves by at least 20% in paired work blocks.

## Phase 7: Interfaces, ingestion, and packaging

**User problem:** Graphify covers broad data and visual exploration, while LLM
Wiki lacks a complete installed product surface.

**Solution:** Add only interfaces that improve agent outcomes or operator
decisions, while preserving offline and source-of-truth guarantees.

**Scope:**

- complete CLI control surface;
- compact and specialized MCP profiles;
- opt-in Streamable HTTP MCP;
- local exception-driven operator console;
- document, PDF, Office, HTML, and URL ingestion;
- opt-in OCR/media transcription with derived-evidence labels;
- query/path/explain and deterministic graph exports;
- portable OKF/evidence bundle export;
- PyPI/source installation, signed platform artifacts, SBOM, upgrade, rollback;
- Windows, Linux, and macOS installation and migration evidence.

**Exit gate:** Every declared Graphify target workflow completes inside LLM Wiki,
offline behavior passes with network blocked, and installation requires no
separate graph or code-memory product.

## Phase 8: Competitive proof and release

**User problem:** Existing smoke and known-item benchmarks cannot justify product
superiority.

**Solution:** Run component, fixed-agent, native-product, and longitudinal
operator tracks with pinned comparators and full lifecycle accounting.

**Comparators:**

- raw LLM with filesystem tools;
- BM25, dense, and hybrid retrieval;
- Graphify;
- codebase-memory-mcp;
- RepoWise where license and installation permit evaluation;
- Mem0 OSS and managed as separate rows;
- Graphiti and Zep as separate rows;
- Letta native-agent track;
- Cognee, ReMe, agentmemory, and ai-memory where adapters are reproducible;
- human-curated handoff and oracle evidence ceilings.

**Suites:**

- Evidence Retrieval;
- LongMemEval, LoCoMo, and BEAM;
- RepoBench-R, CrossCodeEval, and CodeRAG-Bench;
- SWE-bench Verified subset and local executable tasks;
- multi-session workstreams;
- negative claims and evidence safety;
- freshness and incremental equivalence;
- 3,000 reliability fault schedules;
- offline/privacy;
- six-to-eight-week N-of-1 operator study.

**Exit gate:** The public report includes immutable raw ledgers, pinned manifests,
paired clustered confidence intervals, failures, lifecycle token/cost accounting,
and claim eligibility. Marketing text may state only what these gates establish.

## Completion protocol for every phase

A phase is complete only when all conditions hold:

- research document is dated and cited;
- architecture decision is recorded when needed;
- implementation plan contains exact files, tests, and commands;
- each behavior was developed test-first;
- focused tests pass;
- full canonical-runtime suite passes;
- Ruff and structural lint pass;
- production path, migration, rollback, and degraded mode are exercised;
- spec-compliance review passes;
- code-quality review passes;
- benchmark gate passes or the result is explicitly `evidence_pending`;
- docs describe current behavior separately from target behavior;
- no commit, push, release, or cleanup occurs without the corresponding user request.

## Explicit non-completion conditions

The following never count as product completion:

- a deterministic fake adapter;
- a smoke fixture;
- passing only focused tests;
- a feature existing only in a live fallback;
- a graph edge without extraction provenance;
- a retrieval result without repository/generation identity;
- a benchmark against a moving branch;
- a lower token count with materially worse task success;
- a UI that displays data the production index cannot produce correctly;
- a plan whose deferred items include competitor-replacement workflows.
