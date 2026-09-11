# One page ceiling for every reader of `knowledge/`

Date: 2026-09-10. Trigger: audit finding M4. The morning's note
(`docs/research/2026-09-10-one-ceiling-for-every-reader-of-a-journal.md`)
aligned the claim tree, the claims reader and the lint reader on 8 MiB
after a 4.2 MB journal had stopped every compile for three days. The audit
found the rest of the family still apart:

| ceiling | readers |
|---|---|
| 8 MiB | `project_journal.MAX_JOURNAL_BYTES`, `claim_tree_manifest.MAX_CLAIM_TREE_FILE_BYTES`, `claims`, `lint_memory`, `corpus_snapshot.MAX_CORPUS_FILE_BYTES`, `search_memory.MAX_PAGE_BYTES` |
| 4 MiB | `claim_tree_manifest.MAX_GUARDRAIL_SOURCE_FILE_BYTES` (`knowledge/notes`, `knowledge/feedback`), `access_tracking.MAX_ACCESS_PAGE_BYTES`, `compile_memory.MAX_AFTER_IMAGE_BYTES`, `rebuild_memory_index.MAX_PAGE_BYTES` |
| 512 KiB | `repair_backlinks.MAX_PAGE_BYTES` (a backlink target page) |

A note between 4 and 8 MiB is accepted by the corpus, the search index and
the claim tree, and refused by the guardrails snapshot (so every transaction
carrying a guardrails precondition is quarantined), by the compile
after-image, by the index rebuild and by access telemetry; above 512 KiB
backlink repair refuses to touch it. Same class, same directory, same day.

## Sources

1. The morning's note and its sources (Azure event-sourcing, Kurrent on
   snapshots): one bound per kind of file, declared once.
2. `scripts/bounded_io.py` is the reading boundary every one of these
   readers already goes through (`read_stable_bytes`), and it imports
   nothing of the product — the one module all of them can import without
   a cycle.
3. The owner's feedback of this morning: a repeated failure at one stage
   is one defect class; fix the family, not the pair.

## Decision

`bounded_io.MAX_KNOWLEDGE_PAGE_BYTES = 8 * 1024 * 1024` is the ceiling for
one Markdown page under `knowledge/`. Every constant in the table becomes an
alias of it, keeping its local name so existing tests that lower it still
lower the reader they test. Compile input (`MAX_SOURCE_BYTES`), provider
responses, logs and the index file are different kinds of file and keep
their own bounds. `tests/test_a_journal_is_readable_by_every_reader.py`
asserts the whole family equal.

Files: `scripts/bounded_io.py`, `scripts/project_journal.py`,
`scripts/claim_tree_manifest.py`, `scripts/corpus_snapshot.py`,
`scripts/search_memory.py`, `scripts/access_tracking.py`,
`scripts/compile_memory.py`, `scripts/rebuild_memory_index.py`,
`scripts/repair_backlinks.py`, `tests/test_a_journal_is_readable_by_every_reader.py`,
`CHANGELOG.md`, `docs/AUDIT-2026-09-10-memory-and-retrieval.md`.
