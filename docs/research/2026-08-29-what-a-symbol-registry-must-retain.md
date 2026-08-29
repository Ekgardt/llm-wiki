# What a symbol registry must retain — and what it must stop doing when abandoned

**Date:** 2026-08-29 (measurements taken 2026-08-28/29 UTC, across midnight)
**Occasion:** `NEW-110` in `docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md`
**Rule 2 research note.** Fact, inference and uncertainty are separated below.

---

## The question

`import_resolver.build_python_symbol_registry` is the live fallback under every
code tool: `find_callers`, `find_callees`, `find_dead_code`, `get_architecture`,
`index_directory`, `parse_file`. The audit named it the real memory eater and
made two claims:

1. it holds the AST of all 7,545 files (~1.5 GiB per thousand);
2. an abandoned live extraction still allocates to the end.

With `OPS-01` landing a shared local HTTP MCP server (`380bbc8`), one extraction
that exhausts memory takes every agent in the process with it. So both claims
had to be checked against the code on disk, not assumed.

---

## What was already true before this note

**Fact.** Claim (1) was fixed on 2026-08-27 by commit `60f7d6c`, about an hour
*after* the audit remainder was written down in `9e31789` (01:50 vs 02:55 UTC).
The audit line was never updated and reads as if nothing had happened. The fix
did two things: it pruned hidden and cache directories from the walk (the rule
`corpus_snapshot._directory_excluded` already used), and it replaced the
retained `list[(path, module, ast.Module)]` with extracted `_ReExport` records
so each tree lives only for its own file.

**Measured here, 2026-08-28 UTC**, on this repository (which has grown to 59,995
`*.py`, 46,611 of them under `.claude/worktrees/`), peak RSS via
`resource.getrusage(RUSAGE_SELF).ru_maxrss`, subprocess per run, no tracemalloc:

| builder | files | peak RSS | wall | outcome |
|---|---|---|---|---|
| pre-`60f7d6c`, whole vault, `RLIMIT_AS` 4 GiB | 2,614 of ~47,400 | **4,060.0 MiB** | 59.9 s | `MemoryError` |
| current, whole vault | 408 | **69.1 MiB** | 2.6 s | complete |

That is ~1.55 GiB per thousand files, confirming the audit's number exactly.
Isolating retention from pruning, on **identical** file sets:

| root | files | pre-`60f7d6c` | current |
|---|---|---|---|
| `scripts/` | 136 | 264.7 MiB | 66.9 MiB |
| `tests/` | 237 | 272.8 MiB | 53.3 MiB |

**Verified, not asserted:** the two builders produce byte-identical registries
on every hidden-directory-free root of this repository — `scripts/` (10,607
symbols / 136 modules), `tests/` (7,215 / 236), `benchmark/` (897 / 32),
`integrations/` (0 / 0); `symbols == symbols` and `modules == modules` in all
four, with empty symmetric difference.

**Where the remaining 69 MiB goes** (`tracemalloc`, snapshot taken at the moment
of highest live memory during the walk): 29.2 MiB live, of which **26.83 MiB is
a single `compile()` tree** — the peak is one AST at a time, which is the
intended shape. After `gc.collect()` only 5.93 MiB is retained: 1.48 MiB of
symbol strings, 1.02 MiB of the two frozensets, 0.27 MiB of method names, and
2.71 MiB of strings first allocated by `compile` that stay reachable (I did not
establish exactly which — most plausibly interned identifiers; stated as
uncertain).

**Conclusion on claim (1): already closed, now measured.** No further retention
work is warranted — interning or an on-disk intermediate would trade code for
single-digit MiB.

---

## How comparable indexers avoid holding every AST

Three shapes are in use, in increasing cost:

**Streaming per-file extraction, tree released immediately.** The standard
answer for build-once indexes: parse one file, extract definitions/references,
release the tree, stream the records onward. This is what tree-sitter-based
indexers such as `cocoindex-code` and `code-index-mcp` do per source file, and
it is what `60f7d6c` adopted. It works precisely when nothing later needs to
re-visit a tree — true here, because the only second pass is the re-export
fixed point, and that pass needs three strings per `from x import y` line, not
a tree.

**LRU eviction of syntax trees.** Needed when the system *does* re-query trees
interactively. rust-analyzer keeps trees in Salsa and evicts them with an LRU
(`set_lru_capacity`): introducing it took one project from ~7.9 GB to ~2.5 GB,
and moving eviction to the right point took `analysis-stats` from 4,169 MB to
2,496 MB. Its documented weakness is directly relevant: rust-analyzer "does a
lot of scan-like operations, which is a classic pathological case for a naive
LRU cache" — a whole-workspace symbol walk is exactly a scan, so an LRU would
buy this vault nothing over simply dropping each tree.

**On-disk intermediate.** SCIP/LSIF-style indexes move the whole intermediate
out of RAM. This repository already considered and superseded that direction —
the one-shot consent/SCIP/publication tasks were dropped on 2026-07-22 in favour
of the read-only LSP engine — so it is out of scope here.

**Shape taken:** streaming per-file, unchanged from `60f7d6c`. **Its cost:** the
registry cannot answer any question that needs a tree after the pass; anything
new that needs one must re-parse that file. That is the right trade for a
build-once registry and the wrong one for an interactive server, which is why
the LSP path exists separately.

---

## The half that was still open: abandonment

**Fact.** `mcp_server` bounds a code-graph call by *abandoning* it. Its own
comment says why that is not enough:

> `code_graph`'s live extraction takes no deadline and cannot be interrupted
> from outside, so an abandoned run keeps its worker until it finishes on its
> own. The slot cap keeps repeated timeouts from stacking runaway workers.

`_bounded_code_graph_call` raises `TimeoutError` at the deadline; the daemon
thread runs on, holding one of `CODE_GRAPH_WORK_SLOTS = 2`. Unlike `_run_bounded`,
the code-graph worker is started with a bare `threading.Thread` and no
`contextvars` context, so the `_OPERATION_CANCELLED` token does not even reach it.

This is not a gap in the vault's engineering; it is a property of the runtime.
The literature is unanimous: a Python thread cannot be interrupted from outside,
so cancellation is cooperative — the work must check a token in its own loop, and
either break or raise. Language servers hit the same wall from the other side:
"in case of infinite loops, the server becomes unresponsive and can't be stopped
by the client."

**Shape taken:** the idiom this vault already uses, unchanged —
`search_memory._check_generation_stop(deadline, cancelled)`: an absolute
`time.monotonic()` deadline plus a `cancelled` callable, raising `TimeoutError`
with a named message. `build_python_symbol_registry` now takes both as
keyword-only arguments defaulting to `None`, and checks at three points:

- once per directory in `os.walk` — on an unpruned tree the *path list* is the
  first thing that grows without bound, before any parsing;
- once per file, before parsing — the allocation point;
- once per round of the re-export fixed point.

**Its cost:** one `time.monotonic()` and one predicate per file — below the
noise floor of a 2.6 s walk over 408 files. It buys nothing at all unless a
caller passes the arguments, which is the honest limit of this change (below).

**Measured, abandonment in the shape `mcp_server` actually uses** — a daemon
worker, a 1.0 s caller budget, then the caller gives up; 3,000 synthetic files:

| | files parsed after abandonment | worker ran on for | peak |
|---|---|---|---|
| before | **2,750** | **8.96 s** | 31.3 MiB |
| after | **1** | **0.02 s** | 3.9 MiB |

**Correctness:** the registry built by the changed module is identical to HEAD's
on the whole vault (18,787 symbols / 409 modules), `scripts/`, `tests/` and
`benchmark/` — `symbols` and `modules` compared as sets, symmetric difference
empty in all four. Peak RSS for a full run is unchanged at ~60 MiB (the file
count drifts run to run because other agents are writing to this checkout).

---

## The audit line points at the wrong function now

**Fact, measured 2026-08-29 UTC.** The registry is the *first* half of a live
extraction. Every one of its callers follows it with a second pass —
`sorted(directory.rglob("*"))` filtered by `_parsable_workspace_file` — and
that predicate prunes only `{.git, .venv, venv, __pycache__, node_modules}`.
It does **not** prune hidden directories, so it still walks straight into
`.claude/worktrees/`:

- `_parsable_workspace_file` accepts **7,686 files on this vault, 7,261 of them
  under `.claude/`** (the walk alone costs 3.7 s). That 7,686 is almost exactly
  the "7,545 files" the audit line attributes to the registry — the number was
  always this pass's scope.
- `_workspace_call_graph` retains `parsed: list[(path, result)]` for every one
  of them, plus `definitions`, `by_name` and `by_qualified`.
- Measured per-subtree: `scripts/` — 141 files, 12.7 s, peak 93.3 MiB, ~516 KiB
  retained per file; `tests/` — 242 files, 12.9 s, peak 108.9 MiB, ~367 KiB per
  file.
- **`_workspace_call_graph(vault_root)` did not finish in 600 s** and was
  killed. *(Inference, from the per-file figures: ~7,686 files at ~400 KiB is
  on the order of 3 GiB retained and ~700 s — consistent with the timeout, but
  extrapolated, not observed to completion.)*

`find_dead_code` and `get_architecture` reach this in live mode. So after this
change the registry is bounded and cancellable, and the pass immediately after
it is neither. The fix is the same two: give `_parsable_workspace_file` the
hidden-directory rule the registry and the corpus walker already share, and
check `code_graph._check_generation_stop` (line 1287, already written) inside
the `rglob` loops. Both are in `scripts/code_graph.py`, which another agent
owns as this is written (four commits today), so this note names the work
instead of doing it.

## What this note does not claim

- **The defect is not yet closed in production.** Nothing passes the new
  arguments. The capability exists and is proven; the delivery does not.
  `scripts/code_graph.py` and `scripts/mcp_server.py` are owned by other agents
  right now (four commits to `code_graph.py` today; `mcp_server.py` was written
  to at 21:07 while this work ran), so they were deliberately not touched. The
  exact remaining plumbing is reported separately, not guessed at here.
- **Nothing was measured on Windows or macOS.** All numbers are Linux, one
  four-core host, under concurrent load from other agents — wall-clock figures
  vary by 4x between runs for that reason and should be read as orders of
  magnitude. The memory figures were stable across runs.
- **`_SKIPPED_DIRECTORY_NAMES` is a heuristic, not a contract.** A workspace
  that keeps real source under a dot-directory is invisible to this registry.
  That was `60f7d6c`'s deliberate trade and this note does not reopen it.

---

## Sources

- [use salsa's LRU for syntax trees — rust-analyzer PR #1382](https://github.com/rust-lang/rust-analyzer/pull/1382)
- [analysis-stats: run Salsa's LRU at the end of analysis — rust-analyzer PR #19378](https://github.com/rust-lang/rust-analyzer/pull/19378)
- [A Plan for Making Rust Analyzer Faster — rust-analyzer issue #17491](https://github.com/rust-lang/rust-analyzer/issues/17491)
- [salsa RFC0004: LRU](https://github.com/salsa-rs/salsa-rfcs/blob/master/RFC0004-LRU.md)
- [cocoindex-code — AST-based embedded code search](https://github.com/cocoindex-io/cocoindex-code)
- [code-index-mcp](https://github.com/johnhuang316/code-index-mcp)
- [Cancellation Token Pattern in Python](https://medium.com/nuculabs/cancellation-token-pattern-in-python-b549d894e244)
- [The language server with child threads](https://medium.com/dailyjs/the-language-server-with-child-threads-38ae915f4910)
- [`ast` — Abstract Syntax Trees](https://docs.python.org/3/library/ast.html)

## Related

- [[knowledge/notes/read-only-lsp-navigation-engine-decision]] — why the
  interactive path is a separate engine, and why this registry stays build-once.
- [[knowledge/notes/derived-evidence-generation-decision]] — the stored
  generation this live fallback exists to back up.
