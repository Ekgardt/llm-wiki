# Audit 2026-09-10 — memory and retrieval pipeline

Read-only audit of the memory and retrieval pipeline on branch `work` after
fast-forwarding `work-rounds` (HEAD `6fcab30`). No product code or test was
changed; this file is the only output.

Scope: `scripts/search_memory.py`, `retrieval.py`, `reranker.py`,
`embedding_model.py`, `corpus_snapshot.py`, `evidence_graph.py`,
`evidence_graph_builder.py`, `generation_catalog.py`, `compile_memory.py`,
`claims.py`, `claim_tree_manifest.py`, `contradiction_pipeline.py`,
`query_memory.py`, `llm_client.py`, `project_journal.py`,
`session_evidence.py`, `access_tracking.py`, `benchmark/` (LongMemEval and
retrieval stands) and their tests.

Method: the code graph (codebase-memory-mcp, fresh index of this worktree and
of `scripts/`) for callers, dead paths and complexity; pattern search for
`except`, `suppress`, `MAX_*`, `sys.path.insert`, `sleep`, skips, hardcoded
paths; bounded reads of every hit; the owner's gate
(`~/.claude/tools/ccn_gate.py`, lizard 1.23.0) run per file. Every finding
below says how it was verified: **read** (source read), **ran** (a tool was
executed), **traced** (graph callers/callees, then confirmed by grep).

Classification: **[defect]** = verified in source; **[inference]** = the code
is verified, the consequence is reasoned and not measured; **[question]** =
could not be settled from this checkout.

Rules referenced: R1 graph before code, R2 dated research before design,
R3 honesty (no hidden facts, no stale claims), R4 quality / reliability /
speed / efficiency / token economy, R5 CCN ≤ 5, nesting ≤ 2, no branching in
ternaries; and the repository contracts (Markdown authoritative, derived caches
disposable, fail-closed, bounded reads, no silent fallback).

---

## High

### H1 — A second, unreachable retrieval pipeline lives inside `search_memory.py` [defect]
- Status: removed 2026-09-10 (`docs/research/2026-09-10-one-retrieval-pipeline-not-two.md`).
- Rule: R4 (maintainability), R3 (docs describe it as the product).
- Evidence: `scripts/search_memory.py:5256-5326` (`_search_backends`),
  `5490-5535` (`_legacy_search`), `5538-5575` (`_legacy_ranked`),
  `5682-5706` (`_rrf_fuse_triple`), `5466-5477`, `5480-5486`, `2045-2061`
  (`_reranked`), `2110-2126` (`_finalize_results`), `5063-5134`, `5181-5247`.
- What is wrong: `_search_backends` has zero callers in `scripts/`,
  `tests/`, `benchmark/`, `integrations/`; it is the only caller of
  `_legacy_search`, which is the only caller of `_legacy_ranked`,
  `_rrf_fuse_triple`, `_project_boosted_fusion`, `_optional_vector_results`,
  `_legacy_graph_boosts`, `_reranked_or_capped`/`_reranked`,
  `_finalize_results`, `_try_generation_search` and
  `_generation_search_results`. The live product path is
  `search()` → `retrieval.retrieve_via_search_memory` → `retrieval.fuse_rrf`.
  Tests reference `_legacy_search` only to assert it is **not** called
  (`tests/test_search_ranking.py:924, 1781`,
  `tests/test_retrieval_review_blockers.py:69`), while
  `tests/test_search_ranking.py:48-88` still unit-tests the dead
  `_rrf_fuse_triple`. `docs/STRUCTURE.md:481` still calls
  `search_memory.py` "(triple-RRF)"; the triple RRF that runs is
  `retrieval.fuse_rrf` (`retrieval.py:1568-1612`, weights at `345-347`).
- Verified: traced (graph: `_search_backends` callers_total 0; grep of the
  whole worktree for each name), read.
- Fix direction: delete the `_search_backends` subtree and its dead tests,
  keep only the helpers `retrieval.py` imports, and update STRUCTURE.md.

### H2 — `access_tracking.py` fails the owner's complexity gate and swallows every failure [defect]
- Status: fixed 2026-09-10 (`docs/research/2026-09-10-a-page-that-cannot-be-flushed-is-named.md`); gate exit 0 on the file.
- Rule: R5; R4/R3 (silent fallback).
- Evidence: gate output — `_parse_frontmatter_integer` CCN 6 (49-65),
  `flush_access_to_frontmatter` CCN 12 (108-208, nesting 3 at 165-174),
  `_flush_candidates_with_cursor` CCN 13 (211-262, nesting 3 at 240-254),
  `get_access_stats` CCN 15 (270-318, nesting 3 at 305-313);
  `access_tracking.py:104-105`, `205-206`, `220-221`, `237-238`, `257-258`,
  `288-289` bare `except Exception`.
- What is wrong: this is the only file in the scripts area that the gate
  refuses, and it refuses it on eleven counts. Inside, a page whose flush
  raises (oversize, malformed frontmatter, transaction refused) is skipped
  with `continue` at 205 and the export cursor is still advanced at 256, so
  the failure leaves no trace and the page is not retried until the cursor
  wraps.
- Verified: ran (gate and lizard), read.
- Fix direction: split into guard-clause helpers under CCN 5, and record a
  failed page (path + error) instead of `continue`.

### H3 — The retrieval stand `benchmark/run_retrieval_v2.py` fails the gate wholesale [defect]
- Rule: R5.
- Evidence: lizard: 57 functions over CCN 5 in one file; graph: max CCN 54;
  the merge-time gate run named `_run_benchmark_once` (CCN 124, lines
  2932-3550, nesting 4), `_aggregate_reports` (84), `load_corpus` (71),
  `_orchestrate_selection_impl` (47), `_recompute_report_metrics` (44),
  `main` (41), plus ternary chains at 3399-3406, 3497-3548.
- What is wrong: the file that produces the product's retrieval selection
  evidence is the least maintainable file in the audited set; a 620-line
  function with nested fallbacks cannot be reasoned about for
  "did it fall back silently".
- Verified: ran (lizard, gate), read (hook output).
- Fix direction: refactor per gate (pipeline of stage functions, guard
  clauses) before the next selection run; do not edit selection semantics.

### H4 — The compile lock fails open, and a stuck lock is left behind silently [defect]
- Status: fixed 2026-09-10 (`docs/research/2026-09-10-a-lock-lives-as-long-as-its-process-not-thirty-minutes.md`).
- Rule: repository contract fail-closed; R3 (docstring says "never blocks",
  code does something else: runs unlocked).
- Evidence: `scripts/compile_memory.py:4071-4086` (`_acquire_compile_lock`,
  `except Exception: return False`), `4103-4112` (`_release_compile_lock`,
  `except Exception: pass`), `4046-4068` (`main` treats `False` as
  "spawner owns it, proceed").
- What is wrong: if `maybe_compile` cannot be imported or the lock file
  cannot be read, the run proceeds as if a spawner held the lock — two
  compiles can write the same daily. If clearing the lock fails, nothing is
  reported and the next run prints "another compile is running" (4055-4060)
  with no way to see why.
- Verified: read.
- Fix direction: treat a lock error as "do not run" and print the reason;
  report a failed release to stderr and `_mark_finished`.

### H5 — Session evidence loss is still silent [defect]
- Status: fixed 2026-09-10 (`docs/research/2026-09-10-a-lost-session-record-is-written-down.md`).
- Rule: R3/R4; contract "a failed write never breaks capture" does not say
  "and leaves no trace".
- Evidence: `scripts/session_evidence.py:241-249` (`except Exception:
  return None`), `259-263` (the docstring records that this very swallow
  hid every session between 2026-08-24 and 2026-08-26).
- What is wrong: the write can fail again for a new reason (lease conflict,
  DLP refusal, oversize) and the only signal is a `None` the caller ignores.
  Nothing reaches `logs/`, doctor or the capture diagnostics.
- Verified: read.
- Fix direction: keep the no-raise contract, but record `{path, error}` to
  the capture-failure ledger the product already has.

---

## Medium

### M1 — Under any deadline the legacy dense leg is disabled and the trace blames the model [defect]
- Rule: R3 (truthful trace).
- Evidence: `scripts/search_memory.py:4962`
  (`if deadline is not None or not _dense_backend_ready(query): return None`);
  `scripts/retrieval.py:1630-1638` (`None` from a wanted dense backend is
  reported as `dense_unavailable`); `retrieval.py:4167-4173` passes the
  caller's deadline through.
- What is wrong: every MCP call carries a deadline, so on a vault without an
  active generation the semantic leg never runs, and the trace says the same
  thing it says when the model is missing. No comment or decision explains
  the disable.
- Verified: read; [question] whether it is deliberate — no research note
  found.
- Fix direction: either run the legacy dense leg under the deadline like the
  generation leg does, or report a distinct reason (`legacy_dense_disabled`).

### M2 — `docs/USER-GUIDE.md` describes a `--semantic` opt-in that no longer exists [defect]
- Status: fixed 2026-09-10 (`docs/research/2026-09-10-the-status-names-what-the-search-reads.md`).
- Rule: R3.
- Evidence: `docs/USER-GUIDE.md:295, 300-303` ("Plain `search_memory.py`
  always runs BM25. `--semantic` enables vectors"); `scripts/search_memory.py:6118-6127`
  (`--semantic` is `BooleanOptionalAction`, default `True`).
- Verified: read.
- Fix direction: document `--no-semantic` and the default; drop the stale
  example.

### M3 — `--status`, `--rebuild` and the troubleshooting page act on the legacy index the product no longer reads first [defect]
- Status: fixed 2026-09-10 (`docs/research/2026-09-10-the-status-names-what-the-search-reads.md`).
- Rule: R3.
- Evidence: `scripts/search_memory.py:6189-6211` (`_print_index_status`
  opens `cache/index.sqlite` only; `_rebuild_index_cli` builds the legacy
  FTS only); `docs/USER-GUIDE.md:645-647` ("Search returns nothing → rebuild
  the index, check `cache/index.sqlite`"); `retrieval.py:3765-3766`
  (`force_rebuild` also switches that call to the legacy path).
- What is wrong: with an active generation the answer comes from
  `cache/evidence-graph/…`; `--status` says nothing about it and `--rebuild`
  does not touch it, so the documented remedy cannot fix the documented
  symptom.
- Verified: read.
- Fix direction: `--status` reports the active generation (id, vector
  state, chunk count) and `--rebuild` either builds a generation or says it
  only rebuilds the legacy index.

### M4 — The one-page ceiling still disagrees between readers of the same directory [defect]
- Status: fixed 2026-09-10 (`docs/research/2026-09-10-one-page-ceiling-for-every-reader-of-knowledge.md`).
- Rule: R4; the 2026-09-10 note fixed one pair, not the family.
- Evidence: 8 MiB — `claim_tree_manifest.py:17` (claim tree),
  `claims.py:45`, `corpus_snapshot.py:29`, `search_memory.py:79`,
  `project_journal.py:51`. 4 MiB — `claim_tree_manifest.py:24`
  (`MAX_GUARDRAIL_SOURCE_FILE_BYTES`, roots `knowledge/notes` and
  `knowledge/feedback`, line 38), `access_tracking.py:34`
  (`MAX_ACCESS_PAGE_BYTES`), `compile_memory.py:133`
  (`MAX_AFTER_IMAGE_BYTES`). `bounded_io.py:169-170` raises above the limit.
- What is wrong: a note between 4 and 8 MiB is accepted by the claim tree,
  the corpus and the search index, but (a) any transaction that carries a
  guardrails precondition (`markdown_transaction.py:2649-2656` dispatch,
  `8395-8408`, `3354-3362`) is quarantined with
  `precondition_failed`, (b) `build_guardrails` refuses, (c) its access
  telemetry is never flushed (H2, silently), (d) a compile after-image of
  that size is refused. Same class as the 4.2 MB journal that stopped
  compiles for three days (`docs/ISSUES-2026-09-10.md:95-99`).
- Verified: read, traced (`_hash_sources` 105-126 → `read_stable_bytes`).
- Fix direction: one named page ceiling in one module, imported by every
  reader of `knowledge/`; test that the constants are equal.

### M5 — Vector encoding failures are swallowed with no reason, unlike the model load [defect]
- Rule: R3/R4 (the fix in `_get_embedder` was not carried through).
- Evidence: `scripts/search_memory.py:1274-1277` (`_embed_texts`),
  `6063-6066` (`_encoded_page_vectors`), `5942-5945` (`_cached_vectors`),
  `6103-6106` (`_persisted_vector_metadata`) — all `except Exception: return
  None`; contrast `173-206` where the load failure is named once.
- What is wrong: an encode that raises (OOM, tokenizer error, corrupt
  `.npy`) looks exactly like "no vectors yet"; the docstring at 174-181
  describes this failure mode and the fix stopped one function short.
- Verified: read.
- Fix direction: route these through `_note_embedder_unavailable`-style
  reason recording.

### M6 — Corrupt catalog or vectors artifacts degrade with a label and no diagnostic [defect]
- Rule: R4 (doctor blind), contract "silent fallback".
- Evidence: `scripts/retrieval.py:3803-3806` (`_active_manifest_for`,
  any exception → `None` → `generation_unavailable`), `2860-2865`
  (`_generation_dense_hits`, any exception → `generation_vectors_unavailable`),
  `4062-4068` (`_attach_graph`, any exception drops the whole generation),
  `3839-3841` (`generation_corrupt` then `GenerationSealChanged`).
- What is wrong: the reason word reaches the trace; the exception text
  (which artifact, which check) reaches nothing. A nightly build that
  produces an unreadable `vectors.npy` degrades every search to lexical and
  the operator finds it only by reading traces.
- Verified: read.
- Fix direction: write the exception class and artifact name to the
  retrieval telemetry / a bounded log line so doctor can surface it.

### M7 — Every returned row repeats the twelve trace fields and carries `content` [inference]
- Rule: R4 token economy.
- Evidence: `scripts/retrieval.py:3504-3518` (`_legacy_trace_fields`),
  `3633-3644` (`row.update(trace_fields)` per candidate), `3487-3501`
  (`content` in `_LEGACY_DISPLAY_FIELDS`), `3521-3536` (thirteen score
  fields per row).
- What is wrong: a ten-row answer carries the same twelve trace values ten
  times plus per-row score bookkeeping. Whether the MCP layer strips them
  before the agent sees them was not checked here (out of scope).
- Verified: read; consequence at the agent boundary not measured.
- Fix direction: carry the trace once beside the rows; keep `content` only
  for callers that ask.

### M8 — "Bounded" reads bounded at 16 and 64 GiB [defect, impact inference]
- Rule: contract "bounded reads", R4.
- Evidence: `scripts/evidence_graph.py:30-31` (`MAX_DATABASE_BYTES`,
  `MAX_SOURCE_BYTES` = 16 GiB, used at 1288, 1931, 3130, 3458-3471),
  `scripts/generation_catalog.py:160-161` (16 GiB artifact, 64 GiB
  generation), `scripts/evidence_graph_builder.py:77-78` (manifest bound
  aliased to the 16 GiB artifact bound; the comment says neither is a bound).
- What is wrong: `_normalized_source` (1276-1304) keeps each source fully in
  memory after a check that cannot fail; the ceiling exists to satisfy the
  signature, not to bound anything.
- Verified: read.
- Fix direction: derive per-artifact bounds from the sealed manifest sizes
  and delete the GiB constants, or name them "absurdity ceiling" in code.

### M9 — Tests that assert on the clock [defect]
- Rule: R4 (test reliability).
- Evidence: `tests/test_search_ranking.py:624-625` (`time.sleep(0.1);
  assert not freshness.done()`), `tests/test_claims.py:393-394, 435-436`
  (`time.sleep(0.2); assert thread.is_alive()`), while
  `tests/test_retrieval_partial_on_expiry.py:55-64` documents why exactly
  this assumption failed on Windows py3.10.
- What is wrong: "still blocked after N ms" passes on a slow machine for the
  wrong reason and fails on a stalled one for the wrong reason.
- Verified: read; the sleeps in `test_grounded_qa.py:373`,
  `test_generation_catalog.py:3109`, `test_project_journal.py:1571` were
  read and are inside the worker being timed, not the assertion.
- Fix direction: gate on events (`entered.wait`, `release.set`) as the
  claims tests already do for the first half.

### M10 — `sys.path.insert(0, …)` inside functions, on every call [defect]
- Rule: R4 (hidden global state).
- Evidence: `scripts/compile_memory.py:4023, 4079`;
  `scripts/search_memory.py:2048, 5472` (the last two are inside the dead
  subtree of H1).
- What is wrong: `sys.path` grows by one entry per compile-lock check; the
  module already inserted the same path at import time (`search_memory.py:42`).
- Verified: read.
- Fix direction: import at module top; remove the per-call inserts.

### M11 — Work done and thrown away in the scale stand [defect]
- Rule: R4.
- Evidence: `benchmark/run_scale_matrix.py:511-522` (adoption gate computed,
  then the result overwritten), `~480-486` (`truth` is the same call as
  `result`, so `recall_at_10/50` is 1.0 by construction), `run_smoke`
  imports numpy only for `_ = np`.
- Verified: read (gate output and file).
- Fix direction: delete the discarded call; take truth from a different
  backend or drop the recall columns for the exact cell.

### M12 — Test helpers over the gate [defect]
- Rule: R5 (tests are code the owner maintains).
- Evidence: lizard: `tests/test_evidence_graph.py:377` `_damage_v3` CCN 26
  (an `if/elif` ladder of 24 arms), `:774` CCN 10, `:1075` CCN 8;
  graph: `tests/test_context_compiler.py` `_resolved_call_targets` 11,
  `tests/test_claims.py:331` 8, `tests/test_generation_catalog.py` 7 and 6,
  `tests/test_project_journal.py` 7 and 6, `tests/test_claim_schemas.py` 6
  and 6, `tests/test_retrieval_telemetry.py:180` 6.
- Verified: ran.
- Fix direction: a dict of damage → SQL for `_damage_v3`; split the rest.

---

## Low

### L1 — Stale LanceDB references after the 2026-09-07 retirement [defect]
- Status: fixed 2026-09-10.
- Rule: R3. Evidence: `scripts/embedding_model.py:3-4` ("the LanceDB store
  and the LanceDB rebuild"), `scripts/search_memory.py:151-153`,
  `docs/STRUCTURE.md:75` ("FTS5/vector/graph/LanceDB"),
  `docs/USER-GUIDE.md:403-404` ("leave `cache/lancedb/` in place").
  Verified: read. Fix: delete the four mentions.

### L2 — `docs/STRUCTURE.md:489` lists `SETUP-COGNEE.md`, which does not exist [defect]
- Status: fixed 2026-09-10.
- Rule: R3. Verified: ran (`ls docs`). Fix: remove from the list.

### L3 (fixed 2026-09-10) — `search_memory.py:8-11` promises "<10ms / <50ms"; the same file measures the dense leg at about three seconds (`6119-6121`) [defect]
- Rule: R3. Verified: read. Fix: replace with the measured numbers or delete.

### L4 — `llm_client.py:641-648` docstring contains mojibake ("???") [defect]
- Rule: R3. Verified: read. Fix: restore the em dash.

### L5 — `llm_client._timeout_s` (`944-955`) does `int(override)` on the environment value; a bad `MEMORY_LLM_TIMEOUT_S` crashes the caller instead of being refused by name [defect]
- Rule: R4. Verified: read. Fix: validate once at startup.

### L6 — `ContextBudget(model, 32_768, 4_000, 1_024)` is written twice (`compile_memory.py:625`, `1374`) [defect]
- Rule: R4 (two copies of one budget drift). Verified: read. Fix: one constant.

### L7 — `session_evidence.py:176` and `:236` render the transcript twice per write [defect]
- Rule: R4. Verified: read. Fix: render once, test emptiness on the result.

### L8 — `retrieval.py:4006` aliases `GenerationSealChanged` to itself; `retrieve_via_search_memory` (3961-4250) is 290 lines with ten closures and a graph CCN of 23 [defect]
- Rule: R5 in spirit (lizard scores closures separately, so the gate passes).
  Verified: ran, read. Fix: lift the closures to module functions taking a
  small context object.

### L9 — `generation_catalog.py:325-336` `close()` catches `BaseException` per descriptor and re-raises only the first error [defect]
- Rule: R4. Verified: read. Fix: catch `OSError`, raise an `ExceptionGroup`
  or log the rest.

### L10 — `access_tracking.flush_access_to_frontmatter` docstring says "flush all pending"; the cursor path stops at 100 pages / 1 000 candidates (`37-38`, `227-230`), and `slugs = [slug]` (121-124) is a loop of one [defect]
- Rule: R3/R4. Verified: read. Fix: say "bounded" and drop the loop.

### L11 — `search_memory._print_index_status` (`6194`) opens the legacy SQLite file without `mode=ro` or `validate_runtime_file`, unlike every other opener in the file [defect]
- Rule: R4. Verified: read. Fix: use the read-only URI opener.

### L12 — `retrieval._start_optional_worker:277` and `query_memory._detached_provider:171` catch `BaseException` in daemon threads, so `KeyboardInterrupt`/`SystemExit` become "the stage failed" [defect]
- Rule: R4. Verified: read. Fix: catch `Exception`; let the rest propagate.

### L13 — Other benchmark files outside the two stands also fail the gate (from the merge-time gate run) [defect]
- Rule: R5. Graph counts: `run_code_navigation.py` 15 functions over 5
  (max 40), `run_scale_matrix.py` 11 (max 15; gate also lists nesting 5 in
  `main` and four ternary chains), `run_comparative.py` 10 (max 24),
  `generate_python_qualification.py` 1 (`_with_padding` 12, nesting 3),
  `run_contradiction_benchmark.py` 1 (`build_corpus` 10, nesting 4). The
  LongMemEval files (`run_longmemeval.py`, `longmemeval_vault.py`,
  `longmemeval_score.py`, `longmemeval_judge.py`) pass. Verified: ran.

---

## Questions I could not settle

- Q1 — Every transaction that carries a guardrails precondition re-reads and
  hashes all of `knowledge/notes` and `knowledge/feedback` (up to 32 MiB,
  `claim_tree_manifest.py:25, 391-419`). Which transactions carry it, and
  what it costs on the live vault, was not measured (live vault out of
  bounds for this audit).
- Q2 — M1: is disabling the legacy dense leg under a deadline a decision? No
  note or comment found.
- Q3 — `session_evidence._safe_component` truncates the session id to 64
  characters (`34-36`); two ids sharing a 64-character prefix would overwrite
  one record. Real ids are 36 characters; whether any host emits longer ones
  is unknown.
- Q4 — H2: after a swallowed per-page failure the cursor advances
  (`access_tracking.py:256`); is the page expected to be retried on wrap, or
  is the loss accepted?

---

## Counts

By severity: critical 0, high 5, medium 12, low 13, questions 4.

By rule offended (a finding may count twice):
R3 honesty / stale docs — 11 (H1, H4, H5, M1, M2, M3, M5, L1, L2, L3, L4, L10);
R4 quality / reliability / efficiency — 17 (H1, H2, H4, H5, M4, M5, M6, M7,
M8, M9, M10, M11, L5, L6, L7, L9, L10, L11, L12);
R5 complexity — 5 (H2, H3, M12, L8, L13);
repository contracts (fail-closed, bounded reads, truthful trace) — 5 (H4, M1,
M4, M6, M8).

Gate results (owner's `ccn_gate.py`): scripts area — 16 files clean,
`access_tracking.py` refused (4 CCN, 7 nesting); benchmark LongMemEval files
clean; `run_retrieval_v2.py` refused (57 functions over CCN 5).

## Files in scope not read closely

Pattern-driven pass only (constants, `except` sites, entry points, the
functions the graph flagged): `search_memory.py` (6 254 lines),
`retrieval.py` (4 250), `compile_memory.py` (4 466), `evidence_graph.py`
(4 478), `evidence_graph_builder.py` (2 631), `generation_catalog.py`
(3 352), `corpus_snapshot.py` (2 738), `project_journal.py` (2 803),
`query_memory.py` (2 410), `contradiction_pipeline.py` (1 825),
`claims.py` (1 348), `benchmark/run_retrieval_v2.py` (≈5 200).

Not opened at all: `benchmark/longmemeval_judge.py`,
`benchmark/longmemeval_data.py`, `benchmark/locomo_data.py`,
`benchmark/longmemeval_official.py`, `benchmark/longmemeval_hypotheses.py`,
`benchmark/run_consolidation.py`, `benchmark/consolidation_vault.py`,
`benchmark/compare_arms.py`, `benchmark/beam_data.py`,
`scripts/claim_tree_manifest.py` beyond lines 1-45, 105-126, 168-193,
391-419; `scripts/reranker.py` lines 80-180 and 430-596;
`tests/test_longmemeval_benchmark.py`, `tests/test_retrieval_v2_benchmark.py`,
`tests/test_session_evidence.py`, `tests/test_access_tracking.py`,
`tests/test_reranker.py`, `tests/test_compile_transactions.py` (names and
sizes only).
