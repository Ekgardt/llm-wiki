# Argument bindings and route calls, not a data-flow graph

Date: 2026-09-11. Trigger: the owner asked for issue #24 to be finished. Of
its acceptance criteria four remain: `data_flow` and `cross_service` tracing
(section B), cross-repository matching (D2), precise navigation for
TypeScript and Go (B), and the parity run (E). This note covers the first
three; the graph query criterion is already met and is named below.

## Already met: the typed graph query

`scripts/graph_query.py` (CODE-01, research
`docs/research/2026-08-28-bounded-graph-query-mode.md`) answers
`get_architecture mode=query`: a closed JSON pipeline — one start filter, at
most three edge hops, one limit, 4 KiB of query text — over the active
generation, with every ceiling refusing by name. That is the roadmap's "ad-hoc
graph query language … or an equivalent typed query", without a query
language to inject into. No work is needed for it.

## What a data-flow answer will and will not be

A code property graph joins the syntax tree, the control-flow graph and the
data-flow graph (Joern, https://docs.joern.io/code-property-graph/), and a
real data-flow answer needs a fixpoint over the control-flow graph (Clang,
https://clang.llvm.org/docs/DataFlowAnalysisIntro.html; CodeQL separates
local from global flow,
https://codeql.github.com/docs/writing-codeql-queries/about-data-flow-analysis/).
Interprocedural parameter propagation is not free even in that reference
implementation (2026 survey, https://arxiv.org/html/2603.24837v1). Building
one inside this extractor would be a second analysis engine and a second
truth, which the superset contract forbids.

What the roadmap actually asks for is what codebase-memory-mcp answers:
`trace_path mode=data_flow` — "value propagation with args at each hop".
That is a per-call binding, not a fixpoint. So the extractor records, for
each call it already resolves, which caller-visible names are passed to
which parameter of the callee, and a trace follows those bindings hop by
hop. The answer names itself as argument binding, never as data-flow
analysis, so nobody reads more into it than it proves.

## Decision

- **`BINDS_ARGUMENTS` assertions.** For every resolved `CALLS` assertion
  whose call passes a plain name or attribute, the extractor emits one
  `BINDS_ARGUMENTS` assertion from the same source node over the same span,
  carrying the bindings in its `literal` field as `argument->parameter`
  pairs (positional, keyword and `self` included; at most 8 pairs and 256
  bytes, the surplus named as `+N more`). A call whose target is unresolved
  emits nothing: a binding with no callee is not evidence.

  The assertion carries no target node, because the graph contract forbids
  one assertion to carry both a target node and a literal
  (`scripts/evidence_graph.py`, `CHECK ((target_node_id IS NULL) !=
  (literal_json IS NULL))`, enforced again in `_normalized_assertion`). The
  first cut ignored that and was refused at generation build. Relaxing the
  check would let a node reference live in a JSON blob outside the foreign
  key, so the callee is proven the ordinary way instead: the `CALLS`
  assertion of the same call site already names it, and both assertions
  record evidence over the identical byte span of the call. A trace joins
  them by that span. This also removes a duplicate: the earlier shape
  repeated, for 30,617 calls in this repository, an edge the graph already
  held.
- **`HTTP_CALLS` assertions.** A call to `requests`/`httpx`/`aiohttp` whose
  first argument is a literal path, or whose method is named by the
  function (`get`, `post`, `put`, `patch`, `delete`), becomes an
  `HTTP_CALLS` assertion to the `route` node of the same method and path
  when the generation holds one, and an observation naming
  `unresolved_reference` when it does not. Route nodes already exist
  (`code_extractor._routes`, FastAPI and Flask).
- **Cross-repository (D2).** The per-checkout hint table
  (`cache/code-hints/<checkout>.sqlite3`, owner's decision 2026-09-11) gains
  a `route` table (`method`, `path`, handler, file, line) exported by the
  same index build, which moves the file to `code-hints/v2`; a v1 file is
  ignored until the next build rewrites it. A client call whose path matches
  no route of its own repository stays an observation in the generation, and
  the `cross_service` walk matches that observation against the routes of
  every other registered checkout. Such a hop names the handler, the file
  and the repository it lives in, and is not walked further: the other
  generation is never opened, so nothing is claimed about what happens
  inside it.
- **Cost.** One extractor version bump (`code-extractor/v11` →
  `code-extractor/v12`) and one full rebuild of every derived generation,
  which the nightly does on its own. No new MCP tool, no new runtime path,
  no new runtime dependency.

## E. The evidence set the roadmap asks for

Issue #24 section E asks for the parity set to be extended beyond its
thirteen navigation tasks with data-flow, cross-service and
"which tests cover this function" questions. Two files carry that:

- `benchmark/code-parity-v2.json` — the thirteen v1 tasks unchanged plus
  T14/T15 (argument bindings, graded on the exact `argument->parameter`
  pairs for llm-wiki and on the caller-visible names for every other tool,
  since no two tools spell a binding alike) and T16 (which tests exercise
  `fuse_rrf`). Gold was read by hand from the working tree on 2026-09-11 and
  every citation names a line.
- `benchmark/code-parity-cross-service-v1.json` with
  `benchmark/build_cross_service_fixture.py` — two tiny Git repositories,
  one client and one service, because this repository serves no HTTP route
  and a cross-service question against it would be vacuous. X02 deliberately
  asks what happens *inside* the service, which this design does not answer,
  so the boundary is measured rather than assumed away.

No run is included: runs happen only on the owner's explicit permission.

## Bounds

At most 8 bindings per call and 256 bytes of binding text; at most one
`BINDS_ARGUMENTS` and one `HTTP_CALLS` assertion per call site; path literals
bounded at 512 bytes; the existing assertion, observation and evidence
ceilings are unchanged and still refuse by name.

Files: `scripts/code_extractor.py`, `scripts/evidence_graph.py`,
`scripts/code_graph.py`, `scripts/mcp_server.py`,
`tests/test_argument_bindings_and_route_calls.py`,
`tests/test_flow_and_service_edges.py`, `docs/CODE-NAVIGATION.md`,
`CHANGELOG.md`.
