---
type: raw-source
status: active
confidence: high
source_authority: web
date: 2026-09-10
---

# Warm-index answers inside the loop — 2026-09-10

One-sentence summary: issue #24, section A, asks that `callers`, `callees`,
`definition` and `references` answer in under 1 s p50 (3 s p95) on a
1 000-file Python repository from a warm index, and that the index refresh be
incremental and run in the background; this note measures where the time goes
today, names the current practice the fix follows, and fixes the design before
any code changes.

Written under rule 2 before the change. Every number dated 2026-09-10 was
measured on this machine against a synthetic public fixture, never against the
owner's repositories.

## 1. What was measured

Fixture: a generated repository of 1 022 Python files (20 packages × 50 modules,
5 functions and one class each, one cross-package import per module), committed
to Git, indexed through `repository_index.index_repository` into a state root of
its own. Generation: 44.7 MB (evidence 35.6 MB, incremental manifest 5.9 MB,
vectors, search). Timing: 20 repetitions per query, nearest-rank p50/p95, warm
(the process had already answered once).

| step | measured 2026-09-10 |
|---|---|
| full index, 1 022 files | 59.8 s (embedding model loaded, 1 022 rebuilt) |
| incremental index after one edited file | 16.4 s (100 rebuilt, 922 reused) |
| `find_callers` stored path | p50 255 ms, p95 270 ms |
| `find_callees` stored path | p50 249 ms, p95 257 ms |
| MCP `get_architecture mode=callers` | p50 511 ms, p95 529 ms |
| MCP `mode=callees` | p50 249 ms, p95 260 ms |
| MCP `mode=symbol` | p50 1 007 ms, p95 1 043 ms |
| `snippet_for_symbol` | p50 335 ms, p95 627 ms |
| `coverage_for_path` | p50 258 ms, p95 308 ms |
| `detect_repository_changes` (hashes every file) | p50 202 ms |
| CLI `code_graph.py --callers` with a generation | 0.6 s wall |
| CLI `code_graph.py --callers` without a generation | whole-tree re-parse (the 300 s timeout in #24) |

cProfile of one warm `find_callers` (521 ms under the profiler): 458 ms is
`_active_evidence_graph`, that is *opening* the generation, and 60 ms is the
query. Inside the open, `GenerationCatalog._registered_generation` validates the
generation three times per open — twice through `get_active_for_repository`
(selection, then the post-open re-check) and once through
`_admitted_generation` — and every validation hashes all six artifacts
(`_scan_artifacts` → `_hash_artifact`: 18 hashes, 0.28 s of SHA-256), then
`_deadline_seal_unchanged` scans the directory (89 ms), then
`EvidenceGraph.__init__` hashes `evidence.sqlite3` once more to look up the
format receipt.

So the cost is proportional to artifact bytes × opens per answer, not to the
question. `mode=callers` opens twice (`find_callers`, then
`with_trace_callers`), `mode=symbol` about four times. The owner's 2026-09-09
measurement on a 105 MB generation — callers 1 091 ms, snippet 529 ms, symbol
2 135 ms — is the same curve at 2.4× the bytes.

The second finding: the incremental builder already exists and works.
`repository_index.index_repository` builds against the newest registered
generation of the same repository as parent and
`evidence_graph_builder.build_incremental_generation` reuses every record
whose source digest, path and language are unchanged, then follows
invalidation through dependencies (one edited module → 100 rebuilt, because
99 modules import through it). What is missing is only the trigger: nothing
runs it after a commit or on a timer, and `code_graph.py --callers` on an
unindexed repository re-parses the tree instead of saying so.

Third finding, outside section A but reproduced here so it is not lost:
`coverage_for_path` on an indexed file of a foreign repository answers
`indexed=false, freshness=not_indexed, nodes=11` — the inconsistency the owner
reported in #24. Section B.

## 2. Current practice, with sources

**Validate once, then trust stat identity.** Git's index keeps the result of
`lstat(2)` for every path — size, mtime, inode — and compares it against a
fresh `lstat(2)`; only a mismatch makes it read content. The known gap is the
*racily clean* entry, a file modified within the timestamp granularity of the
index write, which Git closes by treating entries whose mtime equals the index
mtime as suspect. Source: <https://git-scm.com/docs/racy-git>. The same
principle applies here with a stronger precondition: a generation is immutable
after registration, so an unchanged stat identity of an artifact that was fully
validated once in this process is sufficient to reuse the validated reader.

**Long-lived read-only SQLite readers over immutable files.** `immutable=1`
makes SQLite skip locking and change detection and is correct only for files
that truly cannot change; the recommended pattern for read-heavy consumers is a
dedicated long-lived reader connection per file rather than open-per-query.
Sources: <https://www.sqlite.org/wal.html> (immutable query parameter),
<https://hoelz.ro/blog/using-sqlites-immutable-and-mode-flags-to-get-around-database-is-locked-error>,
<https://adhdecode.com/articles/sqlite/sqlite-readonly-mode-connection/>.
The vault already opens generations with `mode=ro&immutable=1`; what it does
not do is keep the connection.

**Incremental indexing follows the change set.** SCIP-based navigation
re-indexes only the files that changed after a push; tree-sitter based
indexers (cocoindex, ckb) reprocess only what changed and reuse cached work for
the rest. Sources: <https://sourcegraph.com/blog/announcing-scip>,
<https://github.com/cocoindex-io/realtime-codebase-indexing>,
<https://github.com/nyxCore-Systems/ckb/wiki/Incremental-Indexing>. The vault's
`build_incremental_generation` is already this; the trigger is the gap.

**Report cold and warm separately; p95 needs samples.** Warm-up runs are
discarded and cold and warm results are published separately; a p95 from a
handful of samples is governed by its slowest sample. Sources:
<https://github.com/sharkdp/hyperfine> (`--warmup`, `--prepare`),
<https://oneuptime.com/blog/post/2025-09-15-p50-vs-p95-p99-latency-percentiles/view>.
The numbers above are warm, 20 samples; the acceptance stand in section E
will use more.

## 3. Decision

1. **A process-local reader cache** (`scripts/evidence_reader_cache.py`).
   `code_graph._active_evidence_graph` keeps the validated `EvidenceGraph` of
   the last few directories it opened (bounded, LRU, closed on eviction, no
   state on disk). A cached reader is reused while three stat identities are
   unchanged: `catalog.sqlite3` (any registration or activation rewrites it),
   the generation's `evidence.sqlite3` (immutable, but proven rather than
   assumed), and the checkout's Git state files (`HEAD`, `logs/HEAD`,
   `index`, `packed-refs`; a commit or checkout touches at least one). A
   changed catalog re-runs the full validated open; a changed Git state
   re-resolves the scope, and keeps the reader only if the repository identity
   is the same. Callers keep their `try/finally: graph.close()` shape: the
   cache hands out a lease whose `close()` releases instead of closing. Leases
   serialise access with a re-entrant lock per entry, and the shared
   connection is opened with `check_same_thread=False` only when the sqlite3
   module reports serialized threading; otherwise the cache stays off. The
   per-generation `observation` count that `_store_report` recomputes on every
   answer is memoised in the entry, since the generation cannot change.
2. **The CLI stops re-parsing silently.** `code_graph.py --callers` answers
   from the generation or says that the repository has none, naming
   `get_architecture mode=index`; `--live` opts into the whole-tree scan.
3. **A background incremental refresh with an existing fence.**
   `repository_index.refresh_repository(directory)` decides staleness cheaply —
   the checkout's current commit differs from the generation's
   `repository_scope.git_commit`, or `detect_repository_changes` finds a
   difference — and then runs the ordinary incremental `index_repository`
   with the newest generation's recorded code roots. It is fenced by the
   existing ownership registry under role `doctor`, scope
   `repository:<repository_id>`; the registry's exclusion is
   `PRIMARY KEY(role, scope)`, so it never collides with the vault's global
   maintenance lease and two sessions never build the same repository twice.
   Triggers: (a) a structural MCP code answer on a registered repository whose
   generation is behind the checkout's commit spawns
   `scripts/repository_index.py refresh <dir>` detached through
   `memory_state.spawn_detached`, at most once per (repository, commit) per
   process, and answers now from the generation it has, with a `freshness`
   block that says so; (b) the nightly pass refreshes every registered
   repository whose root still exists, one bounded step. An unregistered
   repository is never indexed automatically — indexing stays the explicit
   operator action the 2026-08-28 decision made it.
4. **Not done here, named so it is not forgotten.** The catalog's triple
   validation per open is left as it is: the cache makes it a once-per-process
   cost, and changing the catalog's own guarantees is a separate decision. The
   coverage inconsistency is section B. Precise `definition`/`references`
   through Pyright are unchanged; their warm cost is the LSP session, which the
   qualification gate already bounds at 20 ms p95 overhead.

Expected outcome, to be measured after the change on the same fixture: every
stored-path answer under 100 ms warm, the CLI unchanged at ~0.6 s, and the
first query after a commit answering from the previous generation while the
refresh runs.

Files: `scripts/evidence_reader_cache.py` (new), `scripts/code_graph.py`,
`scripts/evidence_graph.py`, `scripts/repository_index.py`,
`scripts/mcp_server.py`, `scripts/scheduled_nightly.py`,
`tests/test_evidence_reader_cache.py` (new), `tests/test_repository_index.py`,
`tests/test_code_graph.py`, `docs/CODE-NAVIGATION.md`, `docs/USER-GUIDE.md`,
`docs/STRUCTURE.md`, `CHANGELOG.md`.
