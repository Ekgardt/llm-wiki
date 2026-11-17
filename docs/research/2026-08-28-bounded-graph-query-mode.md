# Multi-hop graph query: a bounded pipeline of steps instead of Cypher

Date: 2026-08-28. Item `CODE-01` of the unified backlog: parity with `query_graph`
in codebase-memory-mcp, meaning arbitrary multi-hop questions to the code graph
("who calls the callers of X", "symbols in module Y of kind function").

## What the competitors offer

- **codebase-memory-mcp `query_graph`** accepts raw Cypher with a ceiling of
  100k rows; its own instruction advises "for broad queries add
  LIMIT in the Cypher itself", which means the cost limit is left to
  the caller (the codebase-memory-mcp instructions on this machine, the
  session's MCP instructions section).
- **Graphify** offers NL queries and token-budgeted subgraphs, not a raw query
  language ([Graphify-Labs/graphify](https://github.com/Graphify-Labs/graphify),
  overview in `docs/research/2026-08-27-number-one-memory-market-research.md`).

## Why not Cypher (and not our own query language)

1. **Injection surface.** A query string assembled from user
   text is an executable plan; Neo4j states plainly that parameterization
   protects only literals, and "not everything in a query is
   parameterizable" ([Protecting against Cypher Injection, Neo4j KB](https://neo4j.com/developer/kb/protecting-against-cypher-injection/)).
   A recent precedent of exactly our shape is an LLM tool that passes
   generated Cypher to the database: [CVE-2024-7042, LangChain
   GraphCypherQAChain](https://www.sentinelone.com/vulnerability-database/cve-2024-7042/).
   OWASP added Cypher injection to ASVS
   ([OWASP/ASVS#1824](https://github.com/OWASP/ASVS/issues/1824)).
2. **Unbounded cost.** An arbitrary graph traversal has no upper
   bound on work; the practice of bounded graph APIs is to compute the cost before
   execution and refuse above a threshold, like GitHub GraphQL with its node
   limits and rate-limit score
   ([GraphQL resource limitations, GitHub Docs](https://docs.github.com/en/graphql/overview/resource-limitations)).
3. **Our engine already refuses honestly.** `EvidenceGraph.find_nodes` /
   `_execute` raise `ValueError("query row ceiling exceeded")` when
   there are more rows than the ceiling; there is no silent truncation by design (measured
   2026-08-28 on `CODE-05`). A query language on top of this must turn
   every ceiling into a named answer, not a bare exception.

## Decision

A `query` mode of the `get_architecture` tool (the 12-tool surface
does not grow; the same technique as `provenance`/`snippet`/`coverage` from
2026-08-28). A new module `scripts/graph_query.py`; the query is a closed
structural JSON form, not text in a query language:

```json
{"start": {"name": "X", "path": "…", "kind": "function"},
 "hops": [{"edge": "calls", "direction": "in"}],
 "limit": 50}
```

- `start`: at least one of `name`/`path`/`kind`; the values go to
  `find_nodes` as bound parameters; there is no SQL concatenation at all.
- `hops`: a list of hops within the input budget and the query deadline
  (the "no more than 3" limit was removed on 2026-09-30; see
  `2026-09-30-traversal-stops-at-the-budget.md`); the edge comes from a closed vocabulary
  (`calls`, `defines`, `imports`, `contains`, `inherits`, `links`,
  `exposes`, the measured edge set of the live generation), the direction is
  `in`/`out`. An unknown edge is a named refusal with the list of
  served edges, not a simulation.
- `limit`: 1..200; the frontier between steps is bounded by the same number, and truncation
  is named in the answer (`frontier_truncated`).
- Each step checks the deadline; each expansion goes through
  `EvidenceGraph.neighbors` with its own row and work ceilings.
  A node whose expansion the engine refused at a ceiling is listed by name in
  `refused_expansions`; the answer stays bounded and honest.

Deliberately absent: NL queries (separate work; the structural form is their
target execution plan), aggregations, property filters inside a step,
negations ("called by nobody" is the existing `find_dead_code`).

## Sources

- [Protecting against Cypher Injection — Neo4j Knowledge Base](https://neo4j.com/developer/kb/protecting-against-cypher-injection/)
- [CVE-2024-7042 — LangChain GraphCypherQAChain injection](https://www.sentinelone.com/vulnerability-database/cve-2024-7042/)
- [Inclusion of Cypher Injection Prevention in ASVS — OWASP/ASVS#1824](https://github.com/OWASP/ASVS/issues/1824)
- [GraphQL resource limitations — GitHub Docs](https://docs.github.com/en/graphql/overview/resource-limitations)
- [Graphify-Labs/graphify (GitHub)](https://github.com/Graphify-Labs/graphify)
- The cbm `query_graph` surface: the codebase-memory-mcp instructions on this machine.
- The gap: `docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md`, `CODE-01`.
