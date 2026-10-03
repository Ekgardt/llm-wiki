# Fact keys collect only the turns they use

Research and decision date: 2026-10-03. Runtime Python 3.12.3, SQLite 3.45.1;
Python 3.10 compatibility is retained. No new dependency, architecture,
runtime path, model, source schema, tool or setting.

## Actual failures

The installed nightly fact-key step failed during breadcrumb collection,
before extraction, with `TimeoutError: corpus collection deadline reached`.
`fact_keys.user_turns` consumes only `daily-evidence`, but `main` collected
the whole note/project/session corpus and filtered it afterward. Collection
also used its default 30-second deadline rather than the step's existing
operator-supplied deadline.

The existing `pruned_directories` contract says the walk never enters named
directories, but only nested children respected it. Two direct-root pruning
tests and the real CLI input-selection test failed before the fix.

After correcting scope, a read of the actual vault exposed a second failure:
its 15,052,338-byte daily was rejected by the general 8 MiB page-read default.
The archive, compile and daily evidence reader already share
`evidence_resolver.MAX_DAILY_BYTES`. The daily-only fact-key caller now passes
that same contract, rather than introducing another numerical threshold or
changing the general page limit. A CLI regression with a daily larger than
the page limit failed before this correction and succeeds afterward.

## Current primary sources

- [SQLite query planning](https://www.sqlite.org/queryplanner.html): avoid
  scanning unrelated data while preserving the selected result.
- [Python 3.10 os/scandir](https://docs.python.org/3.10/library/os.html#os.scandir):
  directory traversal reads entries and metadata; filtering after collection
  does not avoid that work.
- [Apache Arrow 25.0.1 datasets](https://arrow.apache.org/docs/python/dataset.html):
  explicit source selection and early filtering avoid irrelevant I/O. Arrow
  is guidance only; it is not installed or used here.

Official documentation checked on the decision date. Increasing an unrelated
whole-corpus timeout still pays for discarded sources and hides the scope
error. A second custom daily parser duplicates the safe collector. Truncating
the large daily would lose evidence. None is selected.

## Selected implementation

Honor an explicitly pruned direct root before entering either platform's
walker. Default collection retains its existing source selection. The
fact-key caller requests only its existing daily sources by excluding the
three unused direct roots through the existing API. It passes the shared
step deadline and daily read bound. Daily descriptor sealing, stable content,
membership checks, source/span hashes and failed-collection retries remain.
The model prompt, extraction, ledger, store and failure reporting are unchanged.

No source file or session record is deleted. Excluding a source from this
daily-only operation does not exclude it from the memory generation or other
readers. An edited daily during collection is still refused by the regression.

## Measured scope and limits

On one preserved private vault snapshot, full collection read 10,289 sources
and 30,657 chunks in 27.1013 seconds; scoped collection read 34 daily sources
and 7,092 chunks in 2.1292 seconds. Both recognized zero user turns, so equality
of their empty turn sets is not a positive model-quality qualification.
No model was called in this pair. The CLI regression separately verifies two
nonempty user turns, including the large daily case, using a test-only provider.

A current-vault source-only collection then read all 36 dailies and 22,574
chunks in 20.4694 seconds, including the complete large daily, with zero model
calls. It also recognized zero marked user turns. That observation is retained
as a limitation; this repair does not claim to qualify every capture format,
grounded answer quality, the complete nightly pass or the remaining audit.

122 related tests passed; three platform-specific tests skipped. Actual
changed-function Lizard/AST matching has maximum CCN 4; shape and Ruff checks
pass. Installation, installed guards, real step execution and fresh code
navigation are reported separately after they actually run.

Source: `scripts/fact_keys.py`, `scripts/corpus_snapshot.py`,
`tests/test_a_fact_key_points_at_the_turn.py`, `tests/test_corpus_snapshot.py`.
