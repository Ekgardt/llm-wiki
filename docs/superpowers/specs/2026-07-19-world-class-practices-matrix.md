# World-Class Practices Matrix for the Solo-Operator Superset

**Research date:** 2026-07-19

**Purpose:** Freeze the best-fit technical decision for every approved product
capability before implementation. "Best" means the strongest evidence-backed
fit for one person operating many local agents, not the largest feature list or
the strongest vendor claim.

## Evaluation rules

Every decision below was selected using the same order:

1. Prefer primary specifications, source code, papers, and reproducible results.
2. Separate current open-source behavior from managed or marketing claims.
3. Prefer deterministic local computation over an LLM when both can solve the task.
4. Keep authoritative data human-readable and derived indexes disposable.
5. Count ingestion, maintenance, retries, and operator intervention in total cost.
6. Require explicit coverage before making a negative claim.
7. Reject a default that has not beaten the existing default on a frozen local test.

## 1. Evidence identity and publication

| Capability | Best current practice | LLM Wiki decision | Evidence |
|---|---|---|---|
| Source identity | Content hash plus logical identity and repository revision | Bind repository ID, relative path, SHA-256, Git object/commit, branch/worktree identity, and capture time | Git object model [R1], W3C PROV [R2] |
| Repository scope | Stable workspace identity independent of display path | Canonical repository ID plus explicit checkout/worktree instance; paths alone never select evidence | Git worktrees [R3], SCIP symbol identity [R4] |
| Multi-artifact publication | Build immutable content-addressed artifacts, validate, then atomically move one pointer | Keep one catalog pointer; activate only a complete FTS+graph+coverage generation; vectors are absent, complete, or stale | SQLite atomic commit [R5], OCI descriptors [R6] |
| Reader consistency | Pin one snapshot for the full read and reject mixed generations | Seal catalog pointer and artifact hashes before opening; recheck before returning | SQLite isolation [R7] |
| Incremental correctness | Compare incremental state with a clean rebuild, including deletions and renames | Release gate requires exact source inventory parity and at least 99.5% explained query parity | Bazel remote-cache correctness model [R8] |
| Negative claims | Absence requires complete current scope, not an empty top-k result | Return `found`, `not-found-in-complete-scope`, or `unknown`, with coverage and generation | FreshQA false-premise methodology [R9] |
| Provenance | Claim-to-exact-span links with immutable selectors | Every consequential claim carries repository, revision, path, byte span, span hash, generation, authority, and validity | W3C Web Annotation [R10], ALCE [R11] |

## 2. Code intelligence

| Capability | Best current practice | LLM Wiki decision | Evidence |
|---|---|---|---|
| Syntax extraction | Incremental concrete syntax trees | Tree-sitter remains deterministic syntax layer | Tree-sitter [R12] |
| Semantic identity | Compiler-produced cross-file symbol indexes | Use SCIP when an indexer exists; use LSP/Jedi only with explicit capability and provenance | SCIP [R4], LSP 3.17 [R13] |
| Fallback semantics | Preserve unresolved observations instead of guessing | Syntactic, compiler-confirmed, heuristic, and unresolved edges are distinct states | codebase-memory-mcp [R14] |
| Local graph store | Embedded relational graph with strong lexical search | SQLite rollback journal, normalized node/edge tables, and FTS5; no required graph server | SQLite FTS5 [R15], codebase-memory-mcp [R14] |
| Graph query | Bounded read-only subset rather than arbitrary database access | Add constrained query plans with deadlines, row limits, and no writes | codebase-memory-mcp [R14] |
| Code search | Combine exact path/symbol, lexical, semantic, and topology signals | Query router chooses the cheapest sufficient signal and reports fallbacks | Sourcegraph search [R16], BEIR [R17] |
| Repository map | Rank structurally important symbols under a token budget | Generate task-specific maps instead of one static repository dump | Aider repo map [R18] |
| IaC | Parse deployment artifacts as typed topology, not plain text | Add Docker, Compose, Kubernetes, Kustomize, Terraform, CI, and package-manifest extractors | codebase-memory-mcp [R14] |
| Cross-repository relations | First-class workspace contracts | Model package, API, schema, event, deployment, and decision relations with exact evidence | RepoWise [R19] |
| Freshness | Incremental event-driven update plus periodic reconciliation | Optional bounded watcher for low latency; session-start/nightly reconciliation remains mandatory | GitHub file watching constraints [R20] |
| Distribution | One product install, no separate user-managed graph service | Ship the code kernel as an internal signed component with SBOM and attribution | SLSA [R21], SPDX [R22] |
| Language support | Capability matrix, not grammar count | Publish syntax, definitions, imports, calls, types, inheritance, framework, and data-flow support separately | CrossCodeEval [R23] |

## 3. Memory lifecycle

| Capability | Best current practice | LLM Wiki decision | Evidence |
|---|---|---|---|
| Episodic memory | Immutable source episodes with attribution | Preserve raw session events and compact source-linked daily/session cards | Graphiti [R24], ReMe [R25] |
| Semantic memory | Evidence-backed current claims with history | Markdown claims supersede rather than overwrite; graph remains derived | Graphiti [R24], OKF [R26] |
| Procedural memory | Learn from verified outcomes, not plausible descriptions | Promote a workflow only after accepted artifacts or repeated successful evidence | ReMe paper [R27], Reflexion [R28] |
| Prospective memory | Separate future commitments from current facts | Typed triggers, assumptions, expiry, cancellation, and one-shot activation | Letta schedules [R29], A2A tasks [R30] |
| Temporal truth | Bi-temporal valid time and transaction time | Claims record observed, valid, recorded, and superseded intervals with uncertainty | Graphiti/Zep paper [R31] |
| Consolidation | Structured proposal, duplicate/contradiction retrieval, validation, atomic commit | Use create/corroborate/refine/supersede/quarantine; failed inputs are not checkpointed | ReMe [R25], ai-memory [R32] |
| Adaptive links | Dynamic links improve multi-hop recall but must not rewrite evidence | Permit derived A-MEM-style links; never let them mutate authoritative history | A-MEM [R33] |
| Forgetting | Retrieval suppression, expiry, archive, and tombstones before deletion | Archive episodes, exclude superseded claims, decay weak derived signals, preserve receipts | Graphiti [R24], GitHub Copilot Memory [R34] |
| Personalization | Explicit user/project/agent/session scopes with revocation | Keep operator policy separate from project facts; temporary instructions expire | Mem0 [R35] |
| Isolation | Adversarial tests for cross-agent and cross-project leakage | Default-deny scope joins; zero observed leaks is a release gate | agentmemory release history [R36] |

## 4. Context compilation and retrieval

| Capability | Best current practice | LLM Wiki decision | Evidence |
|---|---|---|---|
| Retrieval routing | Different queries need different retrieval plans | Exact, lexical, dense, graph, temporal, Git, project-state, and no-context routes | Self-RAG [R37], CRAG [R38] |
| Rank fusion | RRF is robust without judged training data | Keep RRF default; learned fusion requires a separate validation win | RRF analysis [R39] |
| Parent expansion | Retrieve fine-grained chunks, then provide authoritative parent context | Expand only required sections and exact spans | RAPTOR/parent retrieval evidence [R40] |
| Progressive disclosure | Small orientation first, deeper evidence on demand | L0/L1/L2 plus exact source; never dump the entire vault by default | Letta MemFS [R41], nvk/llm-wiki [R42] |
| Token packing | Complete evidence items under an explicit model budget | Mandatory safety/task items first, then deterministic relevance-per-token utility | Anthropic context engineering [R43] |
| Reranking | Use only for ambiguous candidate sets | Skip exact results; evaluate multilingual cross-encoders on frozen EN/RU/ZH data | Sentence Transformers [R44] |
| Dense search | Exact search until ANN has measured recall and latency advantage | NumPy exact remains default; USearch/LanceDB adoption is evidence-gated | USearch [R45], LanceDB [R46] |
| Contextual prefixes | Deterministic metadata first; generated context only after evaluation | Start with title, heading, project, type, validity, symbol, and revision | Anthropic contextual retrieval [R47] |
| Grounded answers | Retrieve, cite, verify, or abstain | Expose one MCP answer path with deterministic span/hash verification | ALCE [R11], RAGAS as diagnostic only [R48] |
| Prompt injection | Retrieved content is untrusted data | Retrieved text cannot change tools, authority, filters, budgets, or output schema | OWASP LLM01 [R49], MCP security [R50] |

## 5. Solo-operator agent control

| Capability | Best current practice | LLM Wiki decision | Evidence |
|---|---|---|---|
| Durable work unit | Task survives transport/session failure | Separate objective, task, execution, session, workspace, and artifact | A2A task lifecycle [R30], Temporal [R51] |
| Task contract | Explicit scope, artifact, verification, dependencies, and budget | Assignment is active only after capability preflight and acknowledgement | Anthropic multi-agent research [R52] |
| Checkpoints | Event-sourced replay with idempotent side effects | Stable operation IDs, fenced leases, receipts, and uncertain-outcome quarantine | Temporal [R51], LangGraph interrupts [R53] |
| Parallelism | Use only for independently decomposable work | Score parallelizability; bound depth, fan-out, time, and child budget | Anthropic [R52], multi-agent scaling study [R54] |
| Conflict prevention | Detect workspace and semantic overlap before mutation | Combine declared impact, code graph, active leases, decisions, and changed artifacts | Git worktrees [R3], MAST failure taxonomy [R55] |
| Decision propagation | Versioned decisions selectively interrupt affected work | Impact graph classifies unaffected, adapt-at-checkpoint, and pause-now executions | A2A events [R30] |
| Handoff | Artifact and checkpoint references, not repeated prose summaries | Record exact stopping point, hashes, verification, uncertainties, and ownership transfer | Anthropic artifact guidance [R52] |
| Budgets | Hierarchical hard and soft envelopes | Portfolio/project/task/execution/model/tool budgets with child roll-up | LiteLLM budgets [R56] |
| Convergence | Stop spending when new evidence and verification progress flatten | Compare spend against accepted artifact/coverage progress; pause for strategy change | MAST [R55] |
| Operator UX | Exception queue rather than activity wall | Show decisions, blocked value, collisions, drift, and non-converging spend | OpenAI Codex app framing [R57] |
| Capabilities | Least privilege with progressive elevation | Per-execution path/tool/network/secret/side-effect grants and exact-operation approval | MCP authorization [R50], OWASP agentic threats [R58] |
| Audit | Causal chain from objective to accepted artifact | Append-only event IDs link task, context, tools, mutations, verification, and acceptance | OpenTelemetry GenAI conventions [R59] |

## 6. Interfaces, ingestion, and deployment

| Capability | Best current practice | LLM Wiki decision | Evidence |
|---|---|---|---|
| Agent interface | Task-shaped MCP tools with small profiles | Keep a compact default profile and expose specialized profiles without duplicating semantics | MCP tools specification [R60], RepoWise [R19] |
| Remote interoperability | Streamable HTTP MCP with the same authorization policy | Add opt-in local HTTP; stdio remains default and offline | MCP transports [R61] |
| Agent-to-agent | Use task protocol only across independent products/runtimes | Consider A2A adapter later; do not use it for local subagents sharing control state | A2A 1.0 [R30] |
| Operator interface | Local decision cards and drill-down evidence | CLI is complete; console adds portfolio/exception/artifact/audit views | Codex app [R57] |
| Document ingestion | Type-specific deterministic extraction before OCR/LLM | PDF/Office/HTML parsers preserve page/section coordinates and source bytes | Graphify [R62], Docling [R63] |
| Media ingestion | Transcription/OCR are derived assertions with model identity | Add only behind explicit extras and evidence labels | Graphify [R62] |
| Export | Open evidence and graph formats | Markdown/OKF bundle, JSONL evidence, GraphML/JSON graph, SPDX/SBOM | OKF [R26], GraphML [R64], SPDX [R22] |
| Packaging | Reproducible signed artifacts and source install | PyPI/source first, then signed platform artifacts with SBOM and provenance | PyPA packaging [R65], SLSA [R21] |
| Offline behavior | Network blocked as a test condition | All declared local features pass with egress denied; model downloads are explicit setup | OWASP [R58] |

## 7. Evaluation and public claims

| Capability | Best current practice | LLM Wiki decision | Evidence |
|---|---|---|---|
| Retrieval evaluation | Complete qrels, multiple query types, strong lexical baseline | Report evidence-span Recall, nDCG, precision, redundancy, and budget | BEIR [R17] |
| Conversational memory | Category-level temporal, update, abstention, and reasoning tests | Run LongMemEval, LoCoMo, and BEAM with OSS/managed systems separated | LongMemEval [R66], LoCoMo [R67], BEAM [R68] |
| Code retrieval | Cross-file multilingual and repository-level tests | Run RepoBench-R, CrossCodeEval, CodeRAG-Bench, and custom IaC/cross-repo tasks | [R23], [R69], [R70] |
| End task | Executable task outcome outranks retrieval score | Fixed-agent SWE-bench and local tasks use tests as primary judge | SWE-bench [R71] |
| Multi-agent continuity | Longitudinal workstreams with interruptions and model changes | Measure resumed-task success, duplicate work, stale resurrection, and clarification | MAST [R55] |
| Statistics | Paired clustered trials and confidence intervals | Lower confidence bound must cross zero for superiority; predeclare non-inferiority margins | NIST bootstrap guidance [R72] |
| Cost | Count the full lifecycle | Include ingestion, compile, embedding, retrieval, retries, maintenance, and operator attention | Mem0 benchmark disclosure [R73] |
| Reliability | Fault injection with replayable seeds | 300 PR and 3,000 release schedules; zero observed acknowledged-write loss | SQLite testing philosophy [R74] |
| Claim discipline | Smoke and fixture results are never quality claims | Every report records adapter kind, revision, model, hardware, raw ledger, and claim eligibility | ML reproducibility checklist [R75] |

## Decisions that differ from a market leader

| Market pattern | Why it is not selected as the default |
|---|---|
| Neo4j/remote graph as authority | Adds infrastructure and lets probabilistic extraction become the only surviving truth. |
| Always-on daemon | Increases operational burden for a laptop/solo operator. A bounded optional watcher is enough. |
| Managed proprietary memory | Strong benchmark results but weak portability, inspectability, and offline ownership. |
| Always-on dense retrieval | Exact and lexical questions become slower and less predictable. |
| LLM-generated edge acceptance | A plausible edge without deterministic evidence can cause a wrong code edit. |
| Automatic historical rewriting | Breaks provenance and can recursively contaminate prior knowledge. |
| Huge default MCP tool surface | Consumes schema tokens and increases incorrect tool selection. |
| One headline score | Hides safety failures and incompatible quality/cost trade-offs. |

## Reference index

- [R1] Git object model: https://git-scm.com/book/en/v2/Git-Internals-Git-Objects
- [R2] W3C PROV-DM: https://www.w3.org/TR/prov-dm/
- [R3] Git worktree: https://git-scm.com/docs/git-worktree
- [R4] SCIP: https://scip-code.org/
- [R5] SQLite atomic commit: https://www.sqlite.org/atomiccommit.html
- [R6] OCI image specification descriptors: https://github.com/opencontainers/image-spec/blob/main/descriptor.md
- [R7] SQLite isolation: https://www.sqlite.org/isolation.html
- [R8] Bazel remote caching: https://bazel.build/remote/caching
- [R9] FreshQA: https://arxiv.org/abs/2310.03214
- [R10] W3C Web Annotation: https://www.w3.org/TR/annotation-model/
- [R11] ALCE: https://arxiv.org/abs/2305.14627
- [R12] Tree-sitter: https://tree-sitter.github.io/tree-sitter/
- [R13] Language Server Protocol 3.17: https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/
- [R14] codebase-memory-mcp: https://github.com/DeusData/codebase-memory-mcp
- [R15] SQLite FTS5: https://www.sqlite.org/fts5.html
- [R16] Sourcegraph code search: https://sourcegraph.com/docs/code-search
- [R17] BEIR: https://arxiv.org/abs/2104.08663
- [R18] Aider repository map: https://aider.chat/docs/repomap.html
- [R19] RepoWise: https://github.com/repowise-dev/repowise
- [R20] Watchman: https://facebook.github.io/watchman/
- [R21] SLSA: https://slsa.dev/spec/v1.1/
- [R22] SPDX: https://spdx.dev/specifications/
- [R23] CrossCodeEval: https://arxiv.org/abs/2310.11248
- [R24] Graphiti: https://github.com/getzep/graphiti
- [R25] ReMe auto-memory: https://github.com/agentscope-ai/ReMe/blob/main/docs/en/auto_memory.md
- [R26] Google OKF draft: https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md
- [R27] ReMe paper: https://aclanthology.org/2026.findings-acl.829/
- [R28] Reflexion: https://arxiv.org/abs/2303.11366
- [R29] Letta scheduling: https://docs.letta.com/letta-agent/scheduling
- [R30] A2A 1.0 task lifecycle: https://a2a-protocol.org/latest/topics/life-of-a-task/
- [R31] Zep temporal graph paper: https://arxiv.org/abs/2501.13956
- [R32] ai-memory: https://github.com/akitaonrails/ai-memory
- [R33] A-MEM: https://arxiv.org/abs/2502.12110
- [R34] GitHub Copilot Memory: https://docs.github.com/en/copilot/concepts/agents/copilot-memory
- [R35] Mem0: https://github.com/mem0ai/mem0
- [R36] agentmemory: https://github.com/rohitg00/agentmemory
- [R37] Self-RAG: https://arxiv.org/abs/2310.11511
- [R38] CRAG: https://arxiv.org/abs/2401.15884
- [R39] RRF analysis: https://arxiv.org/abs/2210.11934
- [R40] RAPTOR: https://arxiv.org/abs/2401.18059
- [R41] Letta MemFS: https://docs.letta.com/letta-agent/concepts/memfs
- [R42] nvk/llm-wiki: https://github.com/nvk/llm-wiki
- [R43] Anthropic context engineering: https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- [R44] Sentence Transformers rerankers: https://www.sbert.net/examples/cross_encoder/applications/README.html
- [R45] USearch: https://github.com/unum-cloud/USearch
- [R46] LanceDB vector indexing: https://docs.lancedb.com/indexing/vector-index
- [R47] Anthropic contextual retrieval: https://www.anthropic.com/news/contextual-retrieval
- [R48] RAGAS: https://arxiv.org/abs/2309.15217
- [R49] OWASP prompt injection: https://genai.owasp.org/llmrisk/llm01-prompt-injection/
- [R50] MCP security: https://modelcontextprotocol.io/specification/2025-11-25/basic/security_best_practices
- [R51] Temporal workflow execution: https://docs.temporal.io/workflow-execution
- [R52] Anthropic multi-agent research system: https://www.anthropic.com/engineering/multi-agent-research-system
- [R53] LangGraph interrupts: https://docs.langchain.com/oss/python/langgraph/interrupts
- [R54] Multi-agent scaling study: https://arxiv.org/abs/2512.08296
- [R55] MAST: https://arxiv.org/abs/2503.13657
- [R56] LiteLLM budgets: https://docs.litellm.ai/docs/proxy/users
- [R57] OpenAI Codex app: https://openai.com/index/introducing-the-codex-app/
- [R58] OWASP agentic threats: https://genai.owasp.org/resource/agentic-ai-threats-and-mitigations/
- [R59] OpenTelemetry GenAI conventions: https://opentelemetry.io/docs/specs/semconv/gen-ai/
- [R60] MCP tools: https://modelcontextprotocol.io/specification/2025-11-25/server/tools
- [R61] MCP transports: https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
- [R62] Graphify: https://github.com/Graphify-Labs/graphify
- [R63] Docling: https://github.com/docling-project/docling
- [R64] GraphML: http://graphml.graphdrawing.org/specification.html
- [R65] PyPA packaging: https://packaging.python.org/en/latest/
- [R66] LongMemEval: https://arxiv.org/abs/2410.10813
- [R67] LoCoMo: https://arxiv.org/abs/2402.17753
- [R68] BEAM: https://github.com/mohammadtavakoli78/BEAM
- [R69] RepoBench: https://arxiv.org/abs/2306.03091
- [R70] CodeRAG-Bench: https://arxiv.org/abs/2406.14497
- [R71] SWE-bench: https://www.swebench.com/
- [R72] NIST bootstrap handbook: https://www.itl.nist.gov/div898/handbook/eda/section3/eda366.htm
- [R73] Mem0 benchmarks: https://github.com/mem0ai/memory-benchmarks
- [R74] SQLite testing: https://www.sqlite.org/testing.html
- [R75] ML reproducibility checklist: https://www.cs.mcgill.ca/~jpineau/ReproducibilityChecklist.pdf
