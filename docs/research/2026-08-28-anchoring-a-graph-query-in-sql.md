# Anchoring a who-calls query in SQL — 2026-08-28

Research behind the `NEW-116` fix: giving `EvidenceGraph.edges()` optional
`source_node_ids` / `target_node_ids` filters, and choosing the bound on how
many ids one call may name.

Rule 2 note: this covers the design decision I actually made — the **bound**,
and the choice to filter in SQL rather than in Python. The direction (anchor the
query, keep `assertion_id`) was already settled before I started.

---

## What was measured here, on this repository

The live generation at `cache/evidence-graph/` holds:

| quantity | value |
|---|---|
| resolved `CALLS` assertions | **35,313** |
| `function` + `method` nodes | **19,153** |
| worst same-name collision | `__init__`, **296** nodes |
| next worst | `main` 83, `close` 82, `__post_init__` 47, `wait` 40 |

`_store_find_callers` / `_store_find_callees` asked for every `CALLS` row with
`max_rows=10_000` and filtered in a Python loop. 35,313 > 10,000, so
`EvidenceGraph._execute` raised `Evidence Graph query row ceiling exceeded` and
every who-calls question was refused — 8 of 8 in a direct drive, 0 of 13 on the
parity stand.

Local SQLite here is **3.45.1**, `SQLITE_LIMIT_VARIABLE_NUMBER` reports
**250000**, and an `IN (...)` with 32,766 placeholders executes fine.

## What the sources say

**Filtering belongs at the source, not in the client.** Predicate pushdown is
the standard name for exactly the change made here: move the filter as close to
the data as possible so fewer rows are read before filtering
([Starburst](https://www.starburst.io/blog/what-is-predicate-pushdown/),
[MotherDuck](https://motherduck.com/glossary/predicate-pushdown/),
[QuestDB](https://questdb.com/glossary/predicate-pushdown/)). Oracle NoSQL puts
the failure mode plainly: without pushdown *all* rows are retrieved and filtered
at the client
([Oracle](https://docs.oracle.com/en/database/other-databases/nosql-database/21.1/integrations/predicate-pushdown.html)).
That is what the old code did, and the row ceiling is what made it fail loudly
rather than slowly.

Graph-API guidance says the same about unbounded collection reads: a query with
no limit "will attempt to retrieve all matching content", which is a performance
problem, so bound and narrow every collection query
([DataHub GraphQL best practices](https://docs.datahub.com/docs/api/graphql/graphql-best-practices)).

**The parameter bound is a real portability limit, not a style choice.**
`SQLITE_MAX_VARIABLE_NUMBER` defaults to **999 before SQLite 3.32.0**
(2020-05-22) and **32766** from 3.32.0 onward
([SQLite implementation limits](https://sqlite.org/limits.html)). Projects hit
this often enough to patch it deliberately — Gitea raised its own build to 32766
([go-gitea#11696](https://github.com/go-gitea/gitea/pull/11696)) and
sqlite3.dart carries the same request
([simolus3/sqlite3.dart#246](https://github.com/simolus3/sqlite3.dart/issues/246)).

This product supports Python 3.10, which only requires SQLite 3.7.15+, so a
supported machine may well be on the 999 floor even though this one is not.

## The decision

`MAX_NODE_FILTER = 512`.

- Above the measured worst case on real data (296) with room to roughly double.
- Under the historic 999 floor, so the query is portable to the oldest SQLite a
  supported Python 3.10 may carry — not merely to the 3.45.1 measured here.
- A caller naming more ids than that is **refused by name**
  (`target_node_ids cannot contain more than 512 values`), never silently
  truncated. The whole point of the existing ceiling contract is that it refuses
  rather than lies; a filter that quietly dropped ids would answer a different
  question than the one asked.

Two smaller choices, recorded because they are otherwise invisible:

- `None` means *no filter*; an **empty sequence means no node was named, so
  nothing can match** and the call returns `[]` without touching SQLite.
  Collapsing the two would make an empty resolution silently re-read the whole
  edge set — the exact defect being fixed.
- `callers()` / `callees()` were not reused, though they are already
  node-anchored and bounded. They return nodes with depth; the store facade
  needs the `assertion_id` per edge to resolve file/line through
  `_stored_edge_location`. Anchoring `edges()` keeps that column.

## What is not claimed

- The refusal contract is unchanged. `_execute` still fetches `limit + 1` and
  raises. What changed is that ordinary questions stop reaching the ceiling.
- This does not fix the genuine whole-graph aggregates — `_store_find_dead_code`,
  `_store_get_architecture`, `_store_detect_communities`. Those semantically need
  the full edge set (zero-incoming anti-join, per-node caller counts, Louvain)
  and would need SQL aggregation, not symbol scoping. They remain at
  `max_rows=10_000` and will refuse on a graph this size.
- 512 is bounded by portability, not by any measurement of a repository whose
  worst same-name collision exceeds it. On such a repository the call is refused,
  and the refusal names the bound.

Sources: [sqlite.org/limits.html](https://sqlite.org/limits.html) ·
[Starburst](https://www.starburst.io/blog/what-is-predicate-pushdown/) ·
[Oracle NoSQL](https://docs.oracle.com/en/database/other-databases/nosql-database/21.1/integrations/predicate-pushdown.html) ·
[MotherDuck](https://motherduck.com/glossary/predicate-pushdown/) ·
[QuestDB](https://questdb.com/glossary/predicate-pushdown/) ·
[DataHub](https://docs.datahub.com/docs/api/graphql/graphql-best-practices) ·
[go-gitea#11696](https://github.com/go-gitea/gitea/pull/11696) ·
[simolus3/sqlite3.dart#246](https://github.com/simolus3/sqlite3.dart/issues/246)
