# Code no product path reaches is removed

Date: 2026-09-24. Audit item C-12 of `docs/AUDIT-2026-09-24-live.md`.

## Question

After the audit fixes, which functions does no product path reach, and which of
them should go?

## Sources

- Refactoring.Guru, "Speculative Generality" (fetched 2026-09-24,
  https://refactoring.guru/smells/speculative-generality): an unused method is
  removed, but "Before deleting elements, make sure that they aren't used in unit
  tests. This happens if tests need a way to get certain internal information from
  a class or perform special testing-related actions."
- Semantic Versioning 2.0.0 FAQ (fetched 2026-09-24, https://semver.org/): a
  deprecation ships in a minor release, and the functionality is removed in a
  later major release.

## Method

The code graph (`search_graph`, inbound degree) and a word-bounded grep over
`scripts/ benchmark/ integrations/ skills/` gave each candidate's product callers;
a separate grep over `tests/` gave its test users.

## Findings (facts)

1. Seven functions have no caller anywhere, not even a test:
   `generation_catalog._windows_relative_file_descriptor`,
   `_windows_list_directory`, `_windows_handle_identity_candidates`,
   `_windows_stat_matches_any_identity`, `bounded_io.read_stable_utf8`,
   `corpus_snapshot._normalized_text`, `_normalized_values`.
2. Twelve functions are reached only by a test that tests the function itself:
   `MemoryQueue.export_task`, `MemoryQueue.payload_for_execution`,
   `code_navigation._graph_only_candidates`, `retrieval_telemetry.count_events_after`,
   `retrieval_telemetry.record_event` (a one-element wrapper of `record_events`),
   `retrieval.trace_to_dict`, `project_journal.build_handoff`,
   `codex_memory.codex_hooks_feature_state` (a wrapper of the private state reader
   the product calls), `PositionRange.require_nonempty`,
   `context_budget.fits_within_budget`, `EvidenceGraph.code_to_doc`,
   `EvidenceGraph.doc_to_code`.
3. The module-level queue owner of `memory_queue.py` (`_acquire_queue_owner`,
   `_heartbeat_queue_owner`, `_release_queue_owner`, their adopted-vault variants,
   `QueueOwnerLease` and about twenty helpers) is an island: `trace_path` inbound
   from `_acquire_adopted_queue_owner` ends at `_acquire_queue_owner`, which no
   product module calls. Doctor's bounded worker, which a test docstring still
   named as its caller, and the capture worker own the queue through
   `MemoryQueue.queue_owner`. The `queue_ownership` table itself is still read by
   the pre-adoption readers, adoption and doctor, and stays.
4. The rest of the audit's list is a test seam in the source's sense: tests use it
   to run the real processor in process (`_run_processor_inline`), to build or
   publish a generation fixture (`publish_generation`,
   `build_generation_numpy_vectors`), to seed a ready capture intent
   (`publish_capture_intent`), to read the journal or the undo blockers they
   assert on (`read_journal`, `deletion_blockers`, `ClaimIndex.active_records`),
   to prove the writer scanner's set equals the behavioural matrix
   (`check_knowledge_writers.discover_repository_entrypoints`), or they are Reliability v3
   repair and discard operations the contract names (`abort_for_discard`,
   `append_capture_link_resolution`) and the navigation facade's documented
   operations (`resolve_symbol`, `verify_edge`).
5. `compile_memory.py --all` was deprecated in the Unreleased section and has not
   yet shipped in any release.
6. `flush_memory.LEGACY_SENTINELS` is live reply tolerance: `FLUSH_OK` is a current
   tier, and without the other two a reply saying "(no durable content)" would be
   kept as a note.

## Decision (conclusion)

- Remove the seven functions of finding 1 and the twelve of finding 2, and every
  helper, class and constant that only they used (a Windows directory-listing
  layer of about twenty helpers in `generation_catalog.py`, and
  `CapturedSource.captured_bytes`, which nothing read). Tests that only tested a
  removed function go; tests that used one as a tool now use the product path:
  `record_events`, the purge's `_export_task_in_transaction`, the MCP server's
  `_reported_trace`, the handoff rendering `recover_project_handoff` performs,
  and `EvidenceGraph.neighbors`.
- Remove the island of finding 3 and the tests that only tested it
  (`tests/test_every_queue_ownership_handle_is_closed.py`, four tests of
  `tests/test_adopted_source_fence_and_owner.py`, two of
  `tests/test_memory_queue_migration.py`).
- Keep the seams and contract operations of finding 4; removing them would force
  tests to reach private state or would delete a contract operation.
- Keep `--all` until 5.0.0: SemVer removes a deprecated option in a major release,
  after a minor release that ships the deprecation (4.1.0).
- Keep the reply sentinels.

## Edited files

- `scripts/generation_catalog.py`, `scripts/bounded_io.py`,
  `scripts/corpus_snapshot.py`, `scripts/memory_queue.py`,
  `scripts/code_navigation.py`, `scripts/retrieval_telemetry.py`,
  `scripts/retrieval.py`, `scripts/project_journal.py`,
  `scripts/codex_memory.py`, `scripts/code_intelligence.py`, `scripts/context_budget.py`,
  `scripts/evidence_graph.py`
- the tests that used them, and `docs/AUDIT-2026-09-24-live.md`.

## Uncertainty

Dynamic dispatch by string (`getattr`) would hide a caller from both the graph and
grep; the full suite and the MCP end-to-end test are the check for that.
