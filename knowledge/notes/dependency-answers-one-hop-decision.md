---
type: decision
title: "A Dependency Answer Is One Hop Unless Asked Otherwise"
description: "`mode=dependencies` answers the direct dependencies and says how far it walked, instead of returning the whole reachable set nobody asked for."
date: 2026-08-29
confidence: high
source_authority: user
status: active
---
# A Dependency Answer Is One Hop Unless Asked Otherwise

One-sentence summary: `mode=dependencies` answers the direct dependencies and
says how far it walked, instead of returning the whole reachable set nobody
asked for.

## Decision

Date: 2026-08-29. `get_architecture(mode="dependencies")` walks one hop by
default. A `depth` argument takes it further, clamped between one hop and the
walk's existing ceiling of eight. Every answer carries `depth_applied` and
`depth_frontier_open`.

## Why

**The reach was never decided; it was inherited.** The mode always passed the
walk's maximum depth, so no caller could ask a smaller question. On the active
generation, `scripts/retrieval.py` returns 346 rows that way — 59 modules and
287 individual functions and classes — costing 20 735 tokens after shaping.
The question being asked, "which project modules does this file depend on", is
answered by 7 modules for 363 tokens. Fifty-seven times the cost, for an answer
that buries the one asked for.

**One hop is this product's own convention for the same question.**
`graph_neighbors.neighbors` takes `max_hops: int = 1` over the same 1..8 range.
Choosing differently here was inconsistency, not design.

**It is the convention outside too.** codebase-memory's `trace_path` defaults
to a bounded depth of 3. An IDE call hierarchy — JetBrains Rider, and LSP's
`callHierarchy/incomingCalls` — shows one level and expands a node when the
reader asks. Returning a transitive closure by default is the outlier.

**Nothing is silently lost, and that is what makes the change safe.**
`depth_applied` states how far this answer walked. `depth_frontier_open` states
whether the walk stopped at an edge with unvisited neighbours beyond it — not a
claim that more exists, but the difference between "this is all of it" and
"this is as far as you asked". Without those two fields a bounded answer would
be indistinguishable from a complete one, and that indistinguishability was the
only thing making the old default defensible.

## What it cost and bought

Paired on the 13-task code-parity stand against codebase-memory-mcp, same
machine, same generation:

| | before | after |
|---|---|---|
| tokens, whole stand | 40 801 | 11 409 |
| seconds | 256 | 83 |
| correct / partial / wrong | 11 / 1 / 1 | 11 / 1 / 1 |
| T09 dependencies | 20 735 | 363 |
| T08 reverse dependencies | 9 289 | 420 |

Quality is unchanged: both tasks are still graded correct. The ratio against
codebase-memory-mcp falls from 19.3x to 5.36x. In that same run
codebase-memory-mcp itself graded 9/2/2 rather than its usual 11/1/1, which is
its own variance across runs and not a claim about this change.

## Open questions

- The largest remaining cost is `mode=summary` at 6 107 tokens against
  codebase-memory-mcp's 826. Untouched here.
- Whether the other walking modes (`callers`, `callees`, `path`) carry the same
  inherited reach has not been measured.

## Source / Evidence

- `scripts/code_graph.py` — `DEPENDENCY_DEFAULT_DEPTH`, `_dependency_depth`,
  `_dependency_reach`.
- `scripts/mcp_server.py` — the `depth` argument and the two report keys.
- `tests/test_architecture_depth_argument.py` — 17 tests.
- `docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md` — the measurement record.

## Links

- [[knowledge/notes/read-only-lsp-navigation-engine-decision]]
- [[knowledge/notes/derived-evidence-generation-decision]]
