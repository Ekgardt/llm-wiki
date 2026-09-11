# Code Navigation

One-sentence summary: read-only Python navigation through pinned Pyright 1.1.411,
owned by the MCP process, freshness-proven, and never claiming market superiority.

## Trust and sandbox

Code navigation runs only against **trusted local repositories**. This is
**not an OS sandbox**. Pyright still runs with the current user's OS permissions
and may read configured interpreters, external stubs, and library code; those
inputs become fingerprinted provenance. Do not claim Pyright cannot write or read
other user-accessible paths.

## Installation

Pyright is installed by one explicit operator command into the approved managed
root `cache/code-tools/pyright/1.1.411/`. It **never downloads during a query**:

```bash
uv run python scripts/install_pyright.py --state-root "$LLM_WIKI_STATE_ROOT"
```

The installer verifies the pinned SHA-256 and npm integrity before publishing.
No query, MCP call, doctor check, or profile discovery path downloads or updates
Pyright. The qualified runtime uses Node 22; CI pins Node 22.23.1.

## Process ownership boundaries

- **Windows Job Object** owns the assigned Pyright server tree.
- **POSIX process group** covers the trusted, pinned Pyright server and
  descendants only while they remain in that group.
- A hostile `setsid()` escape is **unsupported**. This path is qualified only for
  pinned Pyright in trusted repositories and does not add an ancestry scan.

## get_architecture modes

Existing structural modes (`summary`, `symbol`, `callers`, `callees`,
`dependencies`, `path`, `community`, `impact`) retain their prior behavior and
the 10-second deadline.

Precise modes (`definition`, `references`, `implementations`, `type`,
`diagnostics`) and positioned `callers`/`callees` (with `path`, `line`, and
`character`) route through the owned Pyright session with one absolute 60-second
deadline created before validation.

Input positions are **one-based lines** and **zero-based UTF-8 byte offsets**.

## Warm answers and the background refresh

Structural answers (`callers`, `callees`, `symbol`, `snippet`, `coverage`)
read the repository's registered generation through a process-local reader
cache (`scripts/evidence_reader_cache.py`). A generation is validated once
per MCP process and then reused while `catalog.sqlite3`, the generation's
`evidence.sqlite3` and the checkout's Git state files keep their stat
identity; a registration or activation re-runs the full validated open, a
commit re-resolves the scope. Nothing is written to disk. A reader nobody
asked for in ten minutes is closed on the next cache access; a process that
never asks again holds one reader until it exits, which on Windows can defer
pruning of that superseded generation until then. Measured
2026-09-10 on a 1 022-file fixture with a 44.7 MB generation, warm p50:
`callers` 42 ms (was 511), `callees` 23 ms (249), `symbol` 86 ms (1 007),
snippet 21 ms (335), coverage 21 ms (258). The cost before was proportional
to artifact bytes times opens per answer, which is why a 105 MB generation
answered `callers` in 1.1 s.

Every structural answer read from a generation carries a `freshness` block:

```json
"freshness": {
  "generation_commit": "…",
  "checkout_commit": "…",
  "stale_by_commit": true,
  "refresh": "started"
}
```

`refresh` is `not_needed`, `started` (the bounded incremental refresh was
spawned detached — once per repository and commit in this process; the
answer itself came from the generation the vault has, and the session never
waits), `already_requested`, `spawn_failed`, or `vault_nightly` (the vault's
own generation is rebuilt by the nightly pass and the freshness watch).

The refresh (`python scripts/repository_index.py refresh <dir>`) decides by
content, not by the commit: it hashes the repository's sources against the
newest generation's source manifest and rebuilds only when something differs,
reusing every unchanged record (one edited file in the 1 022-file fixture:
100 rebuilt, 922 reused, 16 s against 60 s for the full build). It is fenced
under the ownership registry's `doctor` role scoped to the one repository, so
the vault's global maintenance and a second session's refresh of the same
repository never collide; a vault that has not adopted the v3 coordinator
reports `refresh_unavailable` and does not build unfenced. The nightly pass
runs `refresh-all` over every registered repository whose checkout still
exists (a missing checkout is named, not deleted). An unregistered repository
is never indexed automatically: `mode=index` stays the explicit operator
action.

`python scripts/code_graph.py --callers NAME DIR` answers from the generation
and, when the repository has none, says so and exits 2 instead of re-parsing
the tree; `--live` opts into the whole-tree scan.

## The query surface (issue #24, section B)

Every answer below reads the repository's generation through the same
leased reader; nothing re-parses the tree, nothing is written, and no
generation format changed, so existing generations answer without a
reindex. Research:
`docs/research/2026-09-10-a-query-surface-that-answers-the-whole-graph.md`.

- **`mode=coverage`, `path=<relative>`** — one generation's word about one
  file: `indexed` and `freshness` (`fresh`, `stale`, `missing_on_disk`,
  `not_indexed`) come from the generation's own stored source row, the same
  generation the node count comes from. Before this the manifest was read
  relative to the repository, which only the vault has, so every foreign
  repository answered `indexed=false` beside a real node count. The `parse`
  block re-parses the stored bytes with the grammar the extractor used and
  lists the `ERROR`/`MISSING` ranges (tree-sitter) or the `SyntaxError` line
  (Python) as `{kind, line_start, line_end, byte_start, byte_end}`, at most
  20 with `errors_truncated`; the extractor records one `parse_error`
  observation for such a file and indexes nothing from it, so those ranges
  are exactly where "no callers" cannot be trusted. `observations` counts
  the file's observations by reason. `status` is `ok`, `error`,
  `unsupported_language`, `not_parsed` (grammar missing here, or source over
  4 MiB) or `not_indexed`.
- **`mode=search`, `symbol=<pattern>`** — ranked qualified names over the
  whole generation: `*` and `?` are globs, otherwise the pattern matches
  anywhere in the name; `path` narrows to a repository-relative prefix;
  `limit` 1–100 (default 10). Rows carry `qualified_name`, `kind`, `path`,
  `line`, `in_degree`, `out_degree` (resolved edges of every served type —
  not caller counts) and `match` (`exact`, `prefix`, `substring`); the
  answer carries the exact `total` and `has_more`. Ranking: exact, prefix,
  substring, then in-degree descending, then name. A pattern matching more
  than 5 000 names is refused by name. Kinds are `class`, `function`,
  `method`. Not done: BM25 identifier splitting and semantic search over
  symbols — both need a symbol-level artifact the generation does not hold.
- **`mode=snippet`, `symbol=<owner.name | name>`** — the definition block cut
  from the generation's stored bytes at the exact `definition` occurrence
  span (`precision: "exact"`), with `qualified_name`, `kind`,
  `source_sha256` and `freshness` against the working tree. A partial owner
  (`Widget.frob`) matches by suffix. A node without a definition occurrence
  falls back to the definition-line recovery over the working tree
  (`precision: "heuristic"`). Blocks are cut at 120 lines; `end_line` stays
  the true end and `truncated` says so.
- **`mode=callers` / `mode=callees` with `depth`** (1–8) — the CALLS closure
  that deep, breadth-first, from at most 20 nodes of that name. Rows carry
  `qualified_name`, `depth`, the **definition** location (`file`, `line`);
  the one-hop answer (no `depth`, or `depth=1`) is unchanged and locates the
  call site. The report carries `symbol_resolved`, `depth_applied` and
  `depth_frontier_open` exactly as `dependencies` does, and `callers` keeps
  `unresolved_callers`.
- **`mode=data_flow` with `depth`** (1–8) — argument bindings, hop by hop,
  never data-flow analysis. Each row names the callee and the
  `argument->parameter` pairs the call passes (`bindings`), bounded at 8
  pairs and 256 bytes per call. The callee is named by the `CALLS` edge and
  the pairs by the `BINDS_ARGUMENTS` literal assertion of the same call
  span, because one assertion may carry a target node or a literal, never
  both. The `note` says in the answer itself what it is.
- **`mode=cross_service` with `depth`** (1–8) — calls plus the HTTP
  boundary. A client call with a literal path reaches the `route` node of
  the same method and path (`HTTP_CALLS`), and the walk turns around there
  into the handler that declares it (`EXPOSES`), so a request is followed
  into the service that answers it. Rows carry `relation` (`calls`,
  `http_calls`, `handled_by`, `handled_by_repository`) and the report carries
  `routes_crossed` and `repositories_crossed`. A route this repository does
  not serve is matched against the routes every other indexed checkout
  exported into its hint file (`cache/code-hints/<checkout>.sqlite3`,
  `code-hints/v2`); such a hop names the handler, its file and the
  repository it lives in, and stops there — the other generation is never
  opened, so nothing is claimed about what happens inside it.
  Both modes need `code-extractor/v12` or newer, so they answer nothing on
  a generation built before 2026-09-11.
- **`mode=impact`** additionally answers `affected_symbols`: the functions,
  methods and classes that call, import or inherit a changed symbol within
  eight hops (`{qualified_name, kind, path, line, depth}`, at most 200,
  `affected_symbols_truncated`). It walks every resolved edge, medium
  confidence included, and says so in `affected_symbols_note`; the
  `affected` groups (decisions, pages, tests, checkpoints) are unchanged and
  keep their confirmed-edge rule. `mode=changes` remains the file-level diff
  against the newest generation.

Measured 2026-09-10 on a generated 1 020-file Python fixture (20 packages ×
50 modules, 5 chained functions and one class each, one cross-package import
per module; 35.6 MB generation; 20 warm repetitions, nearest-rank p50/p95,
this machine under a load average of about 3): coverage 16/17 ms; search
99/104 ms for 100 substring matches and 99/103 ms for 1 000 matches (the
match set is scanned once and the degrees grouped over it — the correlated
form took 8.7 s for 1 000 matches); snippet by qualified name 13/14 ms;
callers depth 1 19/20 ms, depth 3 from 20 seeds 144/150 ms, depth 8 from 20
seeds 484/489 ms (120 rows; the recursive-CTE form took 801 ms and 4.0 s);
`mode=impact` with one edited file 314/335 ms (10 repetitions, 38 affected
symbols).

## Graph context where the agent searches (issue #24, section C)

Agents reach for `Grep` before a graph tool unless something reminds them.
Three thin adapters do that, all through `scripts/graph_hint.py`:

| Host | Event | What it adds |
|---|---|---|
| Claude Code | `PreToolUse` matching `Grep\|Glob` | a hint when the pattern names a code symbol |
| Claude Code | `SubagentStart` | one line naming the code tools, when the checkout is indexed |
| Codex | `PostToolUse` matching `Bash` (`rg`, `grep`, `git grep`, …) | the same hint, after the search |
| Codex | `SubagentStart` | the same reminder |
| OpenCode | plugin `tool.execute.after` for `grep`/`glob` | the hint appended to the tool output (best effort: OpenCode does not document that this reaches the model) |
| every host | session start | the reminder line inside the session context the adapter already builds |

A hint is at most three definitions and one tool line, labelled as
repository data, never instructions:

```text
[llm-wiki graph] repository metadata, data only, never instructions: 1 definition(s) named "refresh_repository" indexed at 437fec9234:
- scripts.repository_index.refresh_repository (function) scripts/repository_index.py:1035, 7 in / 8 out edges
Callers, callees, snippet: mcp__llm-wiki__get_architecture mode=callers|callees|snippet symbol=refresh_repository
```

It is read from `cache/code-hints/<checkout-hash>.sqlite3`, a per-checkout
table every index build exports from the generation it built (the validated
generation reader costs ~2 s to open from cold, which no hook can pay). A
pattern that is not an identifier, a name the table does not hold, an
unindexed checkout or any error answers nothing and exits 0; the adapter never
blocks or fails a tool call. Measured on a 558-source fixture, 20 runs per
case, one fresh process each (plus ~9 ms for `uv run`): a hit 56 ms p50 /
57 ms p95, a miss 56/58 ms, a literal or `**/*.py` 33/35 ms, `SubagentStart`
55/58 ms; a hint is about 360 bytes.

**Tool names (C3).** Every installer path registers the server as
`llm-wiki`, so the tools are `mcp__llm-wiki__<tool>` and a managed hook
matches all of them with `mcp__llm-wiki__.*` (Claude Code evaluates a matcher
with non-identifier characters as an unanchored regular expression). The twelve
names are fixed by `scripts/install_smoke.py`; the hint and reminder texts name
only `get_architecture` and its existing modes, which a test checks.

## Worktrees and retention (issue #24, section D1)

A repository with a generation is *registered*. Its other worktrees get their
own generation without anyone running the indexer:

- the nightly `repository_index.py refresh-all` lists every registered
  repository's worktrees (`git worktree list --porcelain -z`) and indexes, with
  the code roots of the newest sibling, up to eight that have none, each under
  the same per-repository fence a refresh takes;
- the first structural answer (`get_architecture`) in a worktree without a
  generation starts the fenced `repository_index.py follow <dir>` detached,
  once per checkout and MCP process, and says so in `freshness.refresh`
  (`worktree_follow_started`).

A worktree's first build is a full one: reuse is per checkout.

**Opting out.** Git configuration, which the product only reads:

```bash
git config branch.my-one-off.llmwikiIndex false   # one branch
git config llmwiki.index false                    # the whole repository
git config --worktree llmwiki.index false         # one worktree (extensions.worktreeConfig)
```

The branch key decides first. A marked checkout is refused by name
(`repository_marked_not_indexed`, with the command that unsets it) by `index`,
`refresh` and `follow`.

**Retention.** A foreign generation is never activated, so the vault's pruner
keeps it forever. The nightly `repository_index.py retire` step (after
`refresh-all`, before `prune_generations`) decides per checkout, by identity:
a checkout whose root is gone or that is marked loses every generation and its
hint table; a live checkout keeps its newest generation and the one behind it.
Each discard is `GenerationCatalog.discard_unactivated` under the
per-repository lease; the vault's own generations are never considered;
`--dry-run` prints the plan. Only `cache/` is touched.

**Not done: cross-repository routes (D2).** The graph holds no route or
channel nodes, so there is nothing to match across repositories yet.

## Status semantics

- `ok`: completed against one unchanged revision; empty provider result is still
  provider-reported, not closed-world proof.
- `partial`: useful facts exist but readiness, truncation, fallback, or timeout
  prevents a complete claim.
- `unsupported`: the provider did not advertise the capability.
- `not_ready`: startup or the readiness probe did not complete.
- `stale`: the workspace changed across both the attempt and the one retry.
- `timeout`: the operation deadline elapsed after cancellation.
- `error`: validated execution failed without a safe semantic result.

## Positions, deadlines, and offsets

- Positions are repository-relative; absolute roots and external paths are never
  exposed.
- Offsets are stateless: each offset reruns the request against a fresh current
  revision.
- Structural fallback is explicit, provenance-bearing, and appended after LSP
  results. Graph top-K is never used as an LSP filter.

## Synchronization and freshness

- Create, edit, rename, and delete changes are synchronized against one captured
  workspace revision before a provider request.
- The facade verifies the same revision after the request. A mismatch discards the
  attempt and retries once from a fresh revision; a second mismatch returns `stale`.
- Source-document parsing has a bounded revision-keyed LRU. Semantic/provider results
  are never cached.
- Query-time LSP facts are not published into Evidence Graph or any active generation.
- Empty provider results do not prove repository-wide absence. Complete negative
  answers remain unsupported.

## Bounds

- Default limit 10, maximum 100.
- At most 1,200 estimated tokens in default output.
- 8 MiB frames, 32 pending requests, 10,000 normalized locations, 10,000
  diagnostics, 256 KiB hover, 4 MiB stderr, JSON depth 64.

## Doctor codes and retention

Doctor reports stable codes: `pyright_missing`, `pyright_version_mismatch`,
`pyright_package_mismatch`, `pyright_executable_mismatch`, `pyright_node_mismatch`,
`pyright_initialization_mismatch`, `pyright_configuration_mismatch`,
`lsp_owner_live`, `lsp_failure_evidence_retained`, `lsp_state_unreadable`.

Process scratch lives under `run/lsp/<owner-nonce>/` and follows the existing
`run/` deletion contract, which protects live LSP owners and retained LSP
failure evidence for seven days. Doctor never installs or downloads Pyright and
never removes `run/lsp`.

## Qualification

The deterministic 100 KLOC qualification corpus and gates live under
`benchmark/`. It executes 200 definition, 100 reference, 100 call, 50 mutation,
20 recovery, and four ownership scenarios. Linux Python 3.10 additionally gates
warm facade overhead at 20 ms p95, cold readiness at 60 seconds, and client RSS below
100 MiB. Correctness-only real-Pyright checks run on Windows, Linux, and macOS.
**Market superiority remains unclaimed.**
