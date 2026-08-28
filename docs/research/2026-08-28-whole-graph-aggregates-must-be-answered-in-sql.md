# Whole-graph aggregates must be answered in SQL — and the bound must be in the answer

Date: 2026-08-28. Audit item: `NEW-121` (sibling of `NEW-116`).
Scope: `scripts/code_graph.py`, `scripts/evidence_graph.py`.

## The question

`NEW-116` fixed the *symbol-scoped* half of the row-ceiling defect: `edges()`
learned optional `source_node_ids`/`target_node_ids`, so who-calls / what-calls
anchor in SQL. It deliberately did not attempt the other half. Three store
facades ask questions that no symbol can scope:

* `_store_find_dead_code` — which functions have **no** incoming call?
* `_store_get_architecture` — how many callers does **each** function have?
* `_store_detect_communities` — what are the modules of the **whole** call graph?

All three pulled the graph into Python at `max_rows=10_000` and refused, because
this repository's active generation
(`generation-18cfd903a7a4e112-3ce112cb`) holds **35,313 resolved `CALLS`
assertions over 19,153 function+method nodes** (27,146 nodes total, 82
`entry-point` nodes, 0 `route` nodes, 76,044 unresolved observations — measured
directly against `evidence.sqlite3`, twice).

The refusal contract itself is right and is not in question: `_execute` fetches
`limit + 1` and raises `ValueError("Evidence Graph query row ceiling exceeded")`
rather than truncating. The defect is that these three questions had no way to
reach an answer without walking through that ceiling.

## What current practice says

**Aggregate in the store, not in the client.** The consensus across graph-API
guidance is to push field selection and aggregation into the query and return
only what the caller needs, rather than fetching edges and folding them
client-side; Microsoft's own Graph guidance frames over-fetching as the default
failure mode of graph APIs, and The Graph's querying guidance recommends bounded
alternatives to unbounded variable-length traversal. This is exactly the shape
of the fix: `count(DISTINCT …) … GROUP BY`, and an anti-join, instead of
`for edge in graph.edges(...)`.

**Truncation must never be silent.** The 2026 API-design literature is
unusually blunt here: a size limit is acceptable, a *silent* size limit is not —
responses must carry `has_more` / `total_count`-style metadata, because a
consumer (and especially an agent consumer) cannot otherwise distinguish partial
from complete. Two 2026 bug reports (an `api_call` integration truncating at
~2000 characters, Metabase returning invalid JSON) are cited as the failure this
prevents. This vault already holds the same rule from the other direction —
`self-resolving-health-findings-decision` and the `NEW-86` note both say a
bounded answer must name its bound.

**Louvain needs its edge set, and that is affordable here.** The scaling
literature says the pure-Python constraint is memory, not asymptotics —
louvain-igraph "scales well and can be run on graphs of millions of nodes as
long as they can fit in memory", and the shared-memory record (GVE-Louvain)
operates on billion-edge graphs. Our graph is five orders of magnitude smaller.

**SQLite anti-joins need the join written for the planner.** SQLite runs a
correlated `NOT EXISTS` as a nested loop, once per outer row, and its planner
does not always pick the index a human would. Measured here: the obvious
`NOT EXISTS (SELECT 1 FROM assertion a WHERE a.target_node_id = n.node_id AND
a.edge_type='CALLS' AND a.resolution='resolved')` makes the planner choose
`assertion_resolution (resolution=? AND edge_type=?)` — a scan of all 35,313
resolved `CALLS` rows for each of 19,153 nodes, ~676 M row visits. It ran for
**over 6 minutes without finishing** and had to be killed. Rewritten as
`node_id NOT IN (SELECT target_node_id FROM assertion WHERE …)`, SQLite builds
the ephemeral set once: **0.059 s**. Same answer, four orders of magnitude.

## Measurements that decide the design

All against the live active generation, read-only, on this machine.

| question | rows | cost |
|---|---|---|
| function+method nodes with zero incoming `CALLS`, not an `EXPOSES` source | 8,546 | 0.059–0.126 s |
| …the same, minus names starting `test_` (pushed to SQL) | 3,621 | 0.088 s |
| …after the remaining Python conventions (`main`, `__init__`, `test_*.py` basename) | **868** | — |
| nodes with ≥1 incoming caller (the full hotspot list) | 10,607 | 0.112 s |
| top-20 hotspots by distinct callers (`GROUP BY` + `ORDER BY … LIMIT`) | 20 | 0.096 s |
| distinct **undirected** `CALLS` pairs, self-loops excluded | 29,868 | 0.072 s |
| the same as a Python adjacency dict | 17,194 nodes | **4.07 MB** (`tracemalloc`) |
| `_louvain_communities` over that adjacency | 4,078 communities, largest 193 | **1.93 s** |

Three conclusions follow, and each one settles a design choice.

1. **Dead code is a pure anti-join.** The answer (868) is two orders of
   magnitude smaller than the intermediate the old code built (19,153 nodes +
   35,313 edges). Nothing needs bounding beyond the existing ceiling once the
   anti-join is in SQL. The `test_` name prefix is pushed down as *caller data*,
   not as store policy, purely to widen headroom: 8,546/10,000 (17%) is thin,
   3,621/10,000 (2.8×) is not. The basename rule (`test_*.py`) stays in Python,
   where `PurePath(path).name` means exactly what it says; the nearest SQL
   spelling (`LIKE '%/test\_%'`) would also exclude files under a directory
   whose name starts with `test_`, which is a different answer.

2. **Hotspots are a `GROUP BY`, and they must be bounded and say so.** 10,607
   hotspots is not an answer a person or an agent reads; it is a data dump that
   costs 10,607 occurrence lookups to build. The answer becomes the top 100 by
   distinct incoming callers, and the response carries `hotspot_limit` and
   `hotspots_truncated` so the bound is in the answer rather than in the source.

3. **Communities do not need a degraded answer — they need a bigger, named,
   measured ceiling.** The task allowed an honest "computed over the top-N most
   connected nodes"; the measurement says that would be a *worse* answer bought
   for nothing. The full undirected pair set is 29,868 rows / 4.07 MB / 1.93 s.
   Capping to hub nodes would discard most of the 4,078 real communities —
   including, quite possibly, the one the caller asked about. So the fix folds
   the 35,313 assertions into 29,868 weighted undirected pairs **in SQL**
   (`GROUP BY min(src,tgt), max(src,tgt)`) and gives that aggregate its own
   named ceiling, `MAX_AGGREGATE_ROWS = 200_000` — 6.7× headroom, ≈27 MB at the
   measured 136 bytes/pair. The refusal contract is unchanged: `_execute` still
   fetches `limit + 1` and still raises; only the ceiling constant differs, and
   it differs for a reader that returns folded pairs rather than rows of record.
   The file already carries per-purpose bounds of exactly this kind
   (`MAX_WORK = 100_000`, `MAX_VALIDATION_ROWS = 1_000_000`,
   `MAX_NODE_FILTER = 512`), so this is the file's own convention, not a new one.

## What was rejected

* **Raising `MAX_ROWS`.** It is the default ceiling for rows-of-record readers
  (`find_nodes`, `edges`, `occurrences`); raising it would loosen every one of
  them to buy one aggregate.
* **Top-N-most-connected communities.** Rejected on measurement, above.
* **Pushing the `test_*.py` basename rule into SQL.** Rejected on semantics: the
  available SQL spelling is not the same predicate.
* **Registering a SQLite UDF for `basename` from `code_graph`.** Rejected on
  layering: it would put half of one policy in the store connection.
* **Returning `identity_key` instead of `node_id` in communities**, to make the
  answer readable. Measured and rejected: `identity_key` here is a
  `\x1f`-separated blob carrying the repository digest, language, path, owner,
  name and full signature — ~250 bytes each, ~4 MB across the community answer.
  The opacity of community membership is real (the parity stand's T13 asks
  "which module community does `fuse_rrf` belong to?" and cannot be satisfied by
  a list of `code:node:<md5>`), but it is a separate answer-shape decision, not
  this one. Recorded as a finding, not fixed here.

## Sources

- [Best practices for working with Microsoft Graph](https://learn.microsoft.com/en-us/graph/best-practices-concept)
- [Querying Best Practices | The Graph](https://thegraph.com/docs/en/querying/querying-best-practices/)
- [The SQLite Query Optimizer Overview](https://sqlite.org/optoverview.html)
- [Subtleties of SQLite Indexes — Evan Schwartz](https://emschwartz.me/subtleties-of-sqlite-indexes/)
- [`api_call` integration truncates responses with no warning (2026)](https://github.com/odysseus-dev/odysseus/issues/3391)
- [API Pagination Best Practices: Cursor, Offset & Keyset (2026)](https://www.getknit.dev/blog/api-pagination-best-practices)
- [louvain-igraph](https://github.com/vtraag/louvain-igraph)
- [GVE-Louvain: Fast Louvain Algorithm for Community Detection in Shared Memory Setting](https://arxiv.org/abs/2312.04876)

## What the fix measured

Direct drive on this repository, `LLM_WIKI_STATE_ROOT=/home/user/llm-wiki`,
generation `generation-18cfd903a7a4e112-3ce112cb`:

| tool | before | after |
|---|---|---|
| `find_dead_code` | `ValueError: Evidence Graph query row ceiling exceeded`, 2.97 s | 868 candidates, 8.90 s |
| `get_architecture` | same refusal, 1.16 s | 82 entry points, 0 routes, 100 hotspots (`hotspot_limit` 100, `hotspots_truncated` true), 4,078 communities, 7.17 s |
| `detect_communities` | same refusal, 1.19 s | 4,078 communities over 17,194 members, largest 193, 5.69 s |

Opening the generation is 6.3 s of those figures under this machine's load; the
aggregates themselves are 1–3 s. Both symbols the parity stand asks about,
`_flush_started` and `_search_backends`, are in the dead-code answer, and
`mcp_server` is named in the architecture answer.

Parity stand, `--sides llm_wiki llm_wiki_best --directory /home/user/llm-wiki`.
The before column is the same stand run from a detached worktree at HEAD
against the same live generation, so only the code differs:

| | before | after |
|---|---|---|
| `llm_wiki` correct | **5/13** | **8/13** |
| `llm_wiki` tool errors | 5 (T04, T06, T07, T12, T13) | 1 (T04) |
| `llm_wiki` operator-attention events | 5 | 1 |
| `llm_wiki` wrong-but-confident | 3 | 4 |
| `llm_wiki` total tokens | 3,347 | 112,211 |
| `llm_wiki_best` correct | 10/13 | 11/13 |

T06, T07 and T12 flip from refusal to correct. T04 still fails on the unrelated
`node_id` syntax defect this work did not touch.

Two costs are recorded rather than hidden.

**T13 answers and still grades wrong, and the reason is not the aggregate.**
`detect_communities` now computes 4,078 communities in 8.7 s, and then
`mcp_server`'s answer shaper drops the field before it reaches the caller —
the delivered payload is 459 characters, of which the honest part is
`"answer_budget": {"omitted_fields": ["communities"]}`. That is
`answer_budget._is_opaque_collection` doing its job: a list of lists of
`code:node:<md5>` names no symbol, no file and no line, and its own docstring
measures the same 4,078 communities at 199,770 of 208,786 answer tokens. So the
community answer is computed, bounded, truthful about being dropped, and
useless — because community membership is expressed in opaque ids. That is the
answer-shape decision this note rejected above, and it lives in
`scripts/answer_budget.py` and `scripts/mcp_server.py`, outside this change.
By the stand's definitions T13 moves from "operator attention" to
"wrong but confident", which is why that column goes 3 → 4; the answer declares
its omission rather than fabricating, but the stand grades text.

**The dead-code answer costs 51,437 tokens on this repository.** 868 candidates
at ~205 KB. `find_dead_code` takes no symbol argument, so "is `X` dead?" can
only be served by the whole list; the cheap surface for that question is the
`query` mode `llm_wiki_best` uses, at 191–196 tokens. Making the answer cheaper
means changing the tool's schema, which lives in `mcp_server`.
