---
type: decision
title: "Solo-Operator Agent Control Plane and Superset Product"
description: "LLM Wiki is the single local-first memory, code-intelligence, and agent-control product for one operator managing many agents and sessions."
date: 2026-07-19
confidence: high
source_authority: user
status: active
---
# Solo-Operator Agent Control Plane and Superset Product

One-sentence summary: LLM Wiki is the single local-first memory, code-intelligence, and agent-control product for one operator managing many agents and sessions.

## Decision

Date: 2026-07-19.

LLM Wiki will become a functional superset of the normal agent workflows served
by Graphify, codebase-memory-mcp, and the strongest current agent-memory systems.
The installed product must make a separate installation of those systems
unnecessary for its target user: one person coordinating many agents, sessions,
projects, repositories, branches, and worktrees.

The product combines seven planes:

1. Authoritative Markdown, Git, raw episodes, accepted decisions, project
   journals, and accepted artifacts.
2. Immutable, repository-scoped evidence generations containing a coherent
   source inventory, FTS, vectors, code/knowledge/project graph, exact spans,
   coverage, and extractor identity.
3. A native local code-intelligence kernel using deterministic parsing first and
   compiler/LSP/SCIP enrichment where available.
4. Episodic, semantic, procedural, and prospective memory with provenance,
   valid time, transaction time, supersession, and quarantine.
5. One Adaptive Context Compiler for SessionStart, MCP, QA, handoff, impact, and
   task execution.
6. A durable control plane for objectives, tasks, executions, workspaces,
   artifacts, decisions, checkpoints, budgets, capabilities, exceptions, and
   events.
7. A CLI, task-shaped MCP interface, compact project state, and an
   exception-driven operator console. HTTP MCP, broad ingestion, and a bounded
   optional watcher are approved extensions; none may become authoritative or a
   mandatory cloud dependency.

The product is complete only when real paired benchmarks establish at least
non-inferior task success and Pareto-superior overall value for the target user.
Finishing a component plan, passing a smoke fixture, or matching a feature count
does not satisfy this decision.

## North-star outcome

The primary product outcome is accepted and verified work per million lifecycle
tokens and per hour of operator attention. Supporting measures include tokens per
successful task, time to first correct action, re-explanation burden, resumption
success, evidence precision, unsafe-negative rate, freshness, recovery, and
cross-project isolation.

## Non-negotiable constraints

- Local-first and useful with network access blocked.
- No project-operated cloud service is required.
- Markdown, Git, project journals, raw episodes, and accepted artifacts remain
  authoritative; runtime databases are coordination or derived state.
- Every consequential claim is evidence-backed or explicitly uncertain.
- A retrieval miss never proves absence without complete, current coverage.
- Agents receive least privilege and bounded budgets.
- Automatic writes remain recoverable, attributable, and idempotent.
- The system performs no automatic Git push.
- Public superiority claims require pinned, paired, reproducible evidence.

## Rationale

Graphify provides broad graph ingestion and exploration. codebase-memory-mcp
provides fast local code topology. Mem0, Graphiti/Zep, Letta, ReMe, A-MEM,
Cognee, agentmemory, and related systems provide useful memory patterns. None of
them combines human-owned durable knowledge, generation-consistent code evidence,
temporal claims, cross-agent continuity, task commitments, decision propagation,
budget control, and recoverable local operation for one operator.

OpenCode history from 2026-07-09 through 2026-07-19 contained 606 LLM Wiki
sessions, including 514 delegated child sessions. Repeated audits, manual relay,
scope drift, duplicate investigation, and premature completion claims showed that
retrieval alone is insufficient. The product must manage durable commitments and
accepted outcomes, not merely expose chat history or graph primitives.

## Rejected alternatives

- A memory-only product was rejected because it leaves agents repeatedly parsing
  code and reconstructing repository topology.
- A code-graph-only product was rejected because it cannot preserve decisions,
  failed approaches, project continuity, or operator policy.
- A chat dashboard was rejected because operator attention grows with session
  count.
- An autonomous manager agent as the authority was rejected because it adds a
  fallible, opaque decision layer. Agents may recommend; durable state and policy
  remain authoritative.
- Literal feature-count parity was rejected as the definition of superiority.
  Workflow completion, safety, total lifecycle cost, and operator attention are
  the release criteria.
- Required daemons, required remote graph databases, and required cloud services
  were rejected. An optional bounded watcher or HTTP transport may improve
  freshness and interoperability without becoming authoritative or mandatory.

## Consequences

The 2026-07-16 Unified Evidence Retrieval plan remains a historical implemented
subplan, not the final product contract. Deferred Graphify and
codebase-memory-mcp workflows return to the active roadmap. Each subsystem gets a
dated research decision, a test-first implementation plan, a real production-path
test, and a competitive gate before it can be called complete.

Architecture additions are staged so that failure leaves the current
authoritative Markdown/Git record and the last valid evidence generation intact.
The existing runtime deletion and transaction contracts remain in force.

## Source / Evidence

- User approval and product direction, 2026-07-19 OpenCode session.
- OpenCode session inventory, 606 related sessions from 2026-07-09 through
  2026-07-19, read from the local OpenCode session database in read-only mode.
- [[derived-evidence-generation-decision]]
- `docs/superpowers/specs/2026-07-19-world-class-practices-matrix.md`
- `docs/superpowers/plans/2026-07-19-solo-operator-superset-roadmap.md`
- Graphify: https://github.com/Graphify-Labs/graphify
- codebase-memory-mcp: https://github.com/DeusData/codebase-memory-mcp
- Graphiti: https://github.com/getzep/graphiti
- Mem0: https://github.com/mem0ai/mem0

## Related

- [[derived-evidence-generation-decision]]
- [[agent-native-mcp-foundation]]
- [[reliable-memory-stage-2]]
