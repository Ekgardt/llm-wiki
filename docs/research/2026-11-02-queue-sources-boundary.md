# Queue source identities

Approved under the completion mandate, 2026-11-02, baseline bccb6a9c.
Responsibility: pure extraction of source identity from payloads, canonical daily
identity validation, and general task source-link validation. This is not source
IO, migration, authorization, or a new source identity model.

Move the existing identity-key map and helpers `_collect_identity_from_mapping`,
`_collect_source_identity_strings`, `_source_identity_strings`,
`_distinct_source_identities`, `_payload_identity_fields`,
`_identity_paths_or_daily`, `_validated_failure_identity`,
`_require_canonical_daily_id`, `_unsafe_link_shape`, `_unsafe_link_path`,
`_link_pair`, `_normalized_source_link`, `_normalized_source_links`, plus the
three pure MemoryQueue static methods `_validate_source_identity`,
`_validate_failure_identity`, `_source_identity_from_payload` to
`llm_wiki.queue_sources`. Existing static methods become static aliases.
Internal calls resolve to the same owner's functions; no executor import.

Keep `_require_single_source_identity`, `_queue_v3_source_links`, migration error
construction, SQL and transitions in memory_queue. Preserve their error types.
The migration parser requires an unambiguous identity while failure recording
uses the first encountered identity. Daily failure sources and general task
source links have different policies; do not silently unify them.

Evidence: built-in indexed reverse dependencies show migration statements call
`_queue_v3_source_links`; source search shows failure recording and queries use
the pure collectors. No tests patch the three static methods. The graph is
incomplete; direct source/consumer checks supplement it. Evidence files:
`logs/decomposition-queue-source-graph.json`,
`logs/decomposition-queue-source-baseline.txt`.

Research and alternatives: completion-plan sources (Fowler bounded contexts,
Import Linter contracts, PEP 544) checked 2026-11-02 apply to this pure boundary.
An object hierarchy or Protocol adds no needed behavior. Moving migration SQL
with the parser would couple pure validation to operational ownership. Retain
ordinary functions and direct aliases, validate isolated import and unchanged
results, then migration and queue integration. No dependencies/versions change.

Own context includes code, tests, docs, required instructions. Integration tests
remain additional qualification, not evidence of isolated module ownership.

## Qualification

Baseline queue/migration: 143 passed. Boundary red: 2 failures before extraction.
All 16 moved function ASTs match after removing staticmethod decoration and
rewriting the two same-owner MemoryQueue qualifications to local calls.
Expanded run: 262 passed, one new test exceeded CCN; split its two independent
import assertions. Final global CCN/shape and weight checks: 19 passed.
Python 3.10: 193 passed (functional/boundary/integration tests; the tokenizer is
not installed in that interpreter). Own weighted run: 51 passed before the
boundary split; the revised boundary independently passed 3 tests. Full-project
Ruff and diff checks passed. No native Windows/macOS or live installation claimed.
Evidence: logs/queue-sources-{preservation.json,qualification.txt,python310.txt,
complexity-final.txt,boundary-final.txt,all-budgets.json}. Context remains below
100000; the final manifest test emits the exact current total.
