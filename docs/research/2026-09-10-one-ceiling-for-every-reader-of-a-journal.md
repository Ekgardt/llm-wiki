# One ceiling for every reader of a journal

Date: 2026-09-10. Status: applied.

## What broke

The compile has not committed since 2026-09-07. The nightly at 03:00
reports three `draft:claude:<implicit>:validation_error` in twelve seconds;
the automatic compile at 14:36 today says what the validation actually
refused:

```
compile_memory: FAILED — transaction not committed: ValueError: claim index page exceeds 4194304 bytes
```

The page is `knowledge/projects/no-hands/journal.md`, 4 241 615 bytes, 981
checkpoint events. The draft stage derives claims through the claim index
(`_with_derived_claims`), the index rebuild reads every project page under
`knowledge/projects` — `context.md`, `journal.md`, `state.md` — and refuses
the journal, so the draft is recorded as a validation error whatever the
model answered. Three drafts, three refusals, no compile.

## The same defect was fixed once already

`docs/research/2026-09-09-a-journal-rolls-by-size-too.md` found this
journal the day before and raised the claim tree's page cap to the
journal's own ceiling (`claim_tree_manifest.MAX_CLAIM_TREE_FILE_BYTES`,
8 MiB) so "a page the journal accepts is never one the claim tree refuses".
It missed the second reader of the same file: `claims.MAX_CLAIM_PAGE_BYTES`
stayed at 4 MiB, and so did `lint_memory.MAX_LINT_PAGE_BYTES`, which reads
the same three project files for evidence references. The roll-by-size
rule only fires on the next append, and this project has not appended
since 2026-09-08 09:10, so the journal stays at 4.2 MB and the compile
stays refused.

## Decision

Every reader that can meet a project journal accepts what the journal may
be. `claims.MAX_CLAIM_PAGE_BYTES` and `lint_memory.MAX_LINT_PAGE_BYTES`
are now the claim tree's ceiling, imported rather than restated, and one
test holds the four figures together — journal, claim tree, claim index,
lint — so the next reader that restates a smaller bound fails a test
instead of a compile.

Not chosen: rolling dormant journals from maintenance. It would also work,
but the bound disagreement is the defect; a journal under its own ceiling
must be readable by every reader regardless of when it next rolls. The
abandoned `.journal.md.<hex>.tmp` beside it (835 KB, 2026-09-07) is still
not reclaimed; noted again, not fixed here.

Also seen in the clean-checkout suite of f07d672 under load 9–13:
`tests/test_memory_queue_races.py::test_concurrent_workers_claim_each_row_once_per_lease`
failed once at its 120 s join and passed three times alone. Recorded, not
changed: one failure under a load the runner class does not see.

Files: `scripts/claims.py`, `scripts/lint_memory.py`,
`tests/test_a_journal_is_readable_by_every_reader.py`,
`docs/ISSUES-2026-09-10.md`.
