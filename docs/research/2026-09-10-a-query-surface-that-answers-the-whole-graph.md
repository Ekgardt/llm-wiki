---
type: raw-source
status: active
confidence: high
source_authority: web
date: 2026-09-10
---

# A query surface that answers the whole graph — 2026-09-10

One-sentence summary: issue #24, section B, asks for five query answers that
codebase-memory-mcp gives and LLM Wiki does not yet — trustworthy per-path
coverage, ranked whole-graph symbol search, an exact snippet by qualified
name, depth-bounded transitive tracing, and "what does this diff touch" as a
tool mode; this note records what the generation already stores, what the
current practice is, and fixes each design before any code changes.

Written under rule 2 before the change. Section A of the same issue is merged
(`docs/research/2026-09-10-warm-index-answers-inside-the-loop.md`); this note
builds on its reader cache and its measurements. Every number here is measured
on this machine on a synthetic public fixture or read from the code named,
never from the owner's repositories.

## 1. What the generation stores today (fact, read 2026-09-10)

`scripts/evidence_graph.py` schema (lines 225–302):

- `source(source_id, relative_path UNIQUE, sha256, size, media_type,
  language, git_oid, content BLOB NOT NULL)` — the bytes the extractor parsed
  are stored, per file, in the generation.
- `node(node_id, kind, identity_scheme, identity_key, metadata_json)` with
  metadata `{name, owner, path}` and `signature` for functions and methods
  (`scripts/code_extractor.py` 597–625, 1076–1107). `_stored_qualified_name`
  in `scripts/code_graph.py` (1461) is `owner.name`.
- `occurrence(node_id, source_id, role, byte_start, byte_end, line_start,
  line_end)`. The extractor writes exactly one role, `"definition"`, and its
  span is the whole definition node (`code_extractor.py` 542–1130; Python
  `span` at 606/639 is the `FunctionDef`/`ClassDef` node). So the generation
  does carry precise line ranges for every definition — the 2026-08-28
  `symbol_snippet.py` docstring ("no line numbers") was written against node
  metadata only and is out of date.
- `assertion(source_node_id, edge_type, target_node_id, confidence,
  resolution)`: `CALLS`, `DEFINES`, `IMPORTS`, `CONTAINS`, `INHERITS`,
  `LINKS_TO`, `EXPOSES`, plus the documentation edges. No `DATA_FLOWS`, no
  HTTP/route call edge: `route` nodes exist (`_is_route_decorator`, 1409) but
  nothing links a call site to a route.
- `observation(source_node_id, edge_type, target_text, reason)` with reason in
  `parse_error | unsupported_semantics | unresolved_reference |
  ambiguous_target | dynamic_dispatch | missing_dependency`. On a tree-sitter
  parse with `root_node.has_error` the extractor records **one** `parse_error`
  observation spanning the whole file and indexes **nothing** from it
  (`code_extractor.py` 1428–1436); a Python `SyntaxError` does the same
  (1446–1451); a language without a grammar records `unsupported_semantics`.
  The ranges inside the file that failed are not stored anywhere.
- `expected_source` and `coverage` tables exist in the schema (386–414) and
  the builder never writes them.

Readers already there: `find_nodes` (exact `json_extract` equality on name or
path, 3801), `edges` (3826), `occurrences(node_id)` joining `source` for
`relative_path`/`sha256` (3954), `node_locations` (3965), `neighbors` with a
recursive CTE bounded by `MAX_DEPTH 32`, `MAX_WORK 100 000`, `MAX_ROWS
10 000` (4082), `callers`/`callees` = `neighbors` over `CALLS` in/out (4103),
`reachable` breadth-first over named edge types (4156), `python_sources`
(4393), `unresolved` (4340). `SharedEvidenceGraph` (4470) is the leased reader
`code_graph._active_evidence_graph` returns; the lease carries
`generation_id` and `repository_scope.checkout_root`.

## 2. Why `coverage` lies for a foreign repository (fact)

`scripts/path_coverage.py` 24–40 reads
`<directory>/cache/evidence-graph/generations/<id>/source-manifest.json` —
the manifest relative to the **repository** being asked about. Only the
vault keeps its generations there; a registered foreign repository's
generations live under the vault state root
(`repository_index.state_root_path`, `_open_catalog` at
`<state_root>/cache/evidence-graph/catalog.sqlite3`). So for a foreign
repository the manifest is never found, `indexed` is `false`, `freshness` is
`not_indexed`, and the node count in the same answer comes from the
generation that does exist — the `indexed=false, nodes=11` contradiction
the section A note recorded. A negative `callers` answer cannot be trusted
while the coverage answer beside it says the file is not indexed.

## 3. Current practice (sources)

1. **codebase-memory-mcp** (tool contracts read from the connected server,
   2026-09-10): `search_graph` takes `name_pattern` (regex), `query` (BM25
   over camelCase-split identifiers with label boosting) and
   `semantic_query`; every row carries `qn`, `label`, `file`, `lines`, `in`,
   `out` (degree over `CALLS`, `USAGE`, `CALL_REFERENCE`, `INHERITS`,
   `IMPLEMENTS`), and the answer carries `total` and `has_more`.
   `trace_path` walks `calls`, `data_flow`, `cross_service` with `depth`
   (default 3), `direction`, `limit` and a cursor, and states
   `callers_total`/`callees_total` as the full transitive count.
   `get_code_snippet` answers by exact qualified name and carries a
   `coverage_note` naming line ranges that were only partially indexed.
   `check_index_coverage` is per cited path and says "best-effort, never
   proof of completeness". `detect_changes` maps a diff to symbols.
2. **SCIP** (Sourcegraph, <https://github.com/sourcegraph/scip/blob/main/scip.proto>,
   <https://sourcegraph.com/docs/code-search/code-navigation/precise_code_navigation>):
   a `Document` is a list of `Occurrence`s, each with a `range`
   (`[startLine, startChar, endLine, endChar]`), a `symbol` string and
   `symbol_roles` bit flags (`Definition = 1`, `Import`, `WriteAccess`, …).
   Ranges are the index's own bytes, not the working tree's; consumers
   re-anchor when the file changed. This is exactly the shape our
   `occurrence` rows have, so B3 reads them rather than re-finding the
   definition by regex.
3. **tree-sitter error recovery**
   (<https://tree-sitter.github.io/tree-sitter/using-parsers/>, issues
   <https://github.com/tree-sitter/tree-sitter/issues/650>,
   <https://github.com/tree-sitter/tree-sitter/issues/1136>,
   <https://github.com/tree-sitter/tree-sitter/issues/4163>): a parse never
   fails; unparseable input becomes `ERROR` nodes and a required token the
   parser inserted becomes a zero-width `MISSING` node; `root_node.has_error`
   is the cheap whole-tree flag and the ranges come from walking the tree
   (`(ERROR) @e` and `(MISSING) @m` in queries, or a cursor walk). Editors
   (Neovim, Helix, Zed) show exactly those ranges. Our extractor already has
   the flag and throws the ranges away.
4. **LSP 3.17 call hierarchy**
   (<https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_prepareCallHierarchy>):
   `prepareCallHierarchy` → `incomingCalls`/`outgoingCalls` are one hop
   each; the protocol has no depth. Clients iterate, and every serious
   client bounds the iteration. Our `neighbors` CTE already carries a
   depth bound, a work bound and a row bound, so a depth-bounded `callers`
   costs no new machinery.
5. **Graph query languages**: ISO/IEC 39075:2024 GQL
   (<https://www.iso.org/standard/76120.html>) is the standard property-graph
   language; openCypher (<https://opencypher.org/>) is converging on it;
   SQL/PGQ (ISO/IEC 9075-16:2023) puts `MATCH` inside SQL. SQLite has no
   built-in graph pattern matching — the community extensions (graphqlite,
   sqlitegraph, sqlite-graph) each implement a subset over recursive CTEs,
   which is what `neighbors` does by hand. An ad-hoc query language is
   therefore out of scope here, as the issue allows; `mode=query`'s closed
   JSON pipeline (`scripts/graph_query.py`) stays.
6. **GitHub code navigation** is name-based ("search-based") for most
   languages and precise only where a SCIP/stack-graphs indexer exists; its
   symbol search ranks exact name match first, then prefix, then substring —
   the same three tiers B2 uses, because a model asking for `find_callers`
   wants `find_callers` above `_find_callers_live`.

## 4. Options and decisions

### B1 — coverage per cited path

Options: (a) look the manifest up under the state root instead of the
repository — fixes the path but still answers from a second artifact that the
node count does not come from; (b) answer from the generation's own `source`
table, so `indexed`, `freshness` and `nodes` are one generation's word about
one file; (c) write the `coverage` table in the builder — a generation format
change, which invalidates every published generation and is a separate
decision.

Decision: **(b)**. `EvidenceGraph` gains `source_by_path(relative_path)`
(one row: `sha256`, `size`, `language`, `content`) and
`source_observations(relative_path)` (counts by reason for that source, via
`observation → evidence → source`). `coverage_for_path` answers `indexed`
from the row, `freshness` from the stored `sha256` against the file on disk,
`nodes` as before, and a new `parse` block: the stored bytes are re-parsed
with the same grammar the extractor used (`code_graph._get_parser` for
tree-sitter languages, `ast.parse` for Python) and the `ERROR`/`MISSING`
ranges are listed as `{kind, line_start, line_end, byte_start, byte_end}`,
at most 20, with `errors_truncated`. Status values: `ok`, `error`,
`unsupported_language`, `not_parsed` (grammar missing in this process). The
reparse is bounded by the stored size (the extractor already parsed these
bytes once) and by a node-visit ceiling. The manifest reader is removed. The
note stays: a parse without errors is still not proof of completeness.

### B2 — symbol search with ranking

Options: (a) regex in Python over every node — unbounded; (b) SQL `LIKE …
ESCAPE` on `json_extract(metadata_json,'$.name')` with degree counted in the
same statement; (c) FTS over `search.sqlite3` — that artifact indexes prose
chunks of files (`corpus_snapshot.py`, kind `code` chunks), not symbols, so
it answers with text chunks, not qualified names; (d) semantic — the vectors
embed the same chunks, not symbols.

Decision: **(b)** as `get_architecture mode=search`. `EvidenceGraph.search_nodes(pattern,
kinds, path_prefix, max_rows, deadline)`: glob `*`/`?` become `%`/`_`, other
`%`/`_` are escaped; kinds default `function, method, class`; ranking in SQL
`ORDER BY` — exact name, then prefix, then substring, then in-degree
descending, then qualified name; in/out degree are resolved `assertion` rows
over all served edge types; `total` is an exact `COUNT(*)` of the match and
`has_more` says whether the page was cut. Rows carry `qualified_name`, `kind`,
`path`, `line_start`, `line_end`, `in_degree`, `out_degree`. Contract: required
`{directory, mode, symbol}`, allowed plus `{path, limit}` where `path` is a
repository-relative prefix. Not done, stated: BM25 identifier splitting and
semantic search over symbols — both need a symbol-level artifact the
generation does not have.

### B3 — exact snippet by qualified name

Decision: `snippet_for_symbol` resolves `owner.name` (or a bare name) to nodes
with `find_nodes(name=tail)` filtered on `owner`, reads the definition
`occurrence` (`line_start`, `line_end`, byte span) and cuts the block out of
the **stored** bytes of the generation — the range is exact for those bytes
by construction — reporting `precision: "exact"`, `qualified_name`,
`source_sha256` and `freshness` (`fresh`/`stale`/`missing_on_disk` against
the working tree). A node with no definition occurrence, or no generation at
all, falls back to the 2026-08-28 regex over the working-tree file with
`precision: "heuristic"`. The 120-line cut stays and `truncated` keeps
naming it; the true `end_line` is reported even when the text is cut.

### B4 — transitive tracing with a depth bound

Decision: `callers` and `callees` accept `depth` (1..`ARCHITECTURE_MAX_DEPTH`
= 8). Omitted or 1 is the unchanged one-hop path. For depth ≥ 2 the answer
walks `EvidenceGraph.callers/callees` (the bounded `neighbors` CTE) from every
node of that name (at most 20 seeds), rows carry `depth`, and the report
carries `depth_applied` and `depth_frontier_open` exactly as `dependencies`
does, so a cut answer never reads as complete. `data_flow` and
`cross_service`: **not feasible** on the graph as built — there are no
`DATA_FLOWS` edges and no edge from a call site to a `route` node; adding
them is an extractor change and a new generation format, which is a separate
decision. Named here, not started.

### B5 — what the current diff touches

Fact: `mode=impact` (`scripts/impact_analysis.py`) already maps the diff to
changed symbols through `occurrence` spans and walks confirmed reverse edges
up to depth 8, but `_affected_nodes` (731–788) keeps only decisions, pages,
tests and checkpoints and drops every code symbol it reached. `mode=changes`
answers at file level against the newest generation.

Decision: `analyze_impact` gains an additive top-level `affected_symbols`
list — `{qualified_name, kind, path, line, depth}` for every function, method
or class the same walk reaches, bounded at 200 with `affected_symbols_truncated`
— and `mode=impact` with the default `comparison=dirty` is the tool mode.
The `affected` group shape the tests assert is unchanged.

## 5. Bounds, honesty, speed

- Every new reader goes through `_execute` / `_execute_top` with a row bound
  and the caller's deadline; nothing walks without a depth and work ceiling.
- No daemon, no runtime root, no generation format change: every answer
  reads what published generations already hold, so no reindex is required.
- Latencies are to be measured after the change on the section A fixture
  (1 022 Python files, 44.7 MB generation, 20 warm repetitions, p50/p95) and
  recorded in `docs/CODE-NAVIGATION.md` with those conditions.

Files: `scripts/evidence_graph.py`, `scripts/path_coverage.py`,
`scripts/symbol_snippet.py`, `scripts/symbol_search.py` (new),
`scripts/code_graph.py`, `scripts/impact_analysis.py`,
`scripts/mcp_server.py`, `tests/test_path_coverage.py`,
`tests/test_symbol_snippet.py`, `tests/test_symbol_search.py` (new),
`tests/test_query_surface.py` (new), `tests/test_code_graph.py`,
`tests/test_impact_analysis.py`, `tests/test_mcp_server.py`,
`docs/CODE-NAVIGATION.md`, `docs/USER-GUIDE.md`, `docs/STRUCTURE.md`,
`CHANGELOG.md`.

## 6. Measured after the change (2026-09-10)

Fixture: a generated repository of 1 020 Python files (20 packages × 50
modules, 5 chained functions and one class each, one cross-package import
per module), indexed in 56.7 s into a 35.6 MB generation (9 061 nodes,
17 000 assertions). 20 warm repetitions, nearest-rank p50/p95, load average
about 3 on this machine.

| answer | p50 | p95 |
|---|---|---|
| `coverage_for_path` | 15.7 ms | 17.2 ms |
| `search_symbols`, 100 substring matches | 99.2 ms | 103.6 ms |
| `search_symbols`, 1 000 matches (exact name `run`) | 99.2 ms | 102.9 ms |
| `search_symbols`, glob `Thing*` (1 000) | 99.3 ms | 105.1 ms |
| `snippet_for_symbol` by qualified name | 12.6 ms | 13.5 ms |
| `find_callers` depth 1 | 19.3 ms | 20.0 ms |
| `find_callers` depth 3, 20 seeds | 143.9 ms | 149.5 ms |
| `find_callers` depth 8, 20 seeds, 120 rows | 484.1 ms | 489.0 ms |
| MCP `mode=search` | 96.9 ms | 98.7 ms |
| MCP `mode=callers depth=3` | 163.7 ms | 166.8 ms |
| MCP `mode=impact`, one edited file, 38 affected symbols (10 reps) | 314.3 ms | 335.2 ms |

Two designs were replaced on measurement before the commit. The correlated
degree subquery was served by the planner through `assertion_resolution`,
a scan of every resolved edge per matched node: 1 057 ms for 100 matches
and 8 667 ms for 1 000; grouping the degrees over the matched set and joining
back answers in 72 ms. The `neighbors` recursive CTE carried its visited set
as a string down every path: 801 ms at depth 3 and 4 040 ms at depth 8 from
20 seeds; `reachable`'s breadth-first walk answers in 144 ms and 484 ms.

A `*` pattern over the 7 000 code symbols is refused by name at the 5 000
ceiling rather than ranked. Not measured: the owner's repositories.
