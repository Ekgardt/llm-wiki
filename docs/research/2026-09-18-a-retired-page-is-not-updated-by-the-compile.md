# A retired page is not updated by the compile

Dated 2026-09-18. A consequence of this round's M-A6 fix, found while deleting the dead
`existing_knowledge_snapshot` (whose only surviving test asserted that superseded pages are
not fed to the compiler — an invariant that is no longer true of the live path).

Files: `scripts/compile_memory.py`,
`tests/test_the_compile_decides_what_the_snapshot_already_knows.py`.

## What was found

- `_live_knowledge_pages` snapshots every page under `knowledge/notes/` except archived
  directories, with no status filter, so a page marked `status: superseded` is both a
  compile context source and a write target. Every other reader of the vault applies
  `page_status.is_retired`: the corpus collector, the search page collector, the index
  rebuild, the tier build, the guard rails.
- Before this round, a draft that proposed `create` for a superseded slug was refused and
  the whole plan failed. Since the snapshot now decides the action, the same draft becomes
  an `update` — and an update appends a dated section to the page. That is a compile
  writing into a page the vault has marked as history, which rule 12 of `CLAUDE.md`
  forbids in as many words: "Decisions are immutable: supersede, never edit in place."

## Practice on this date

- The project's own contract is the source here: `CLAUDE.md` §4 rule 12 — "When a new fact
  conflicts with an existing page, mark the old `status: superseded` and add a
  `superseded_by: [[<new-slug>]]` link — never delete. … Decisions are immutable:
  supersede, never edit in place." Retrieval already excludes retired pages, so an update
  written into one would also be invisible to every reader.
- The shape of the remedy is the one the compile already uses for a claim it cannot admit
  (`_admitted_candidates`): drop the part that cannot be written, name it on stderr, and
  let the rest of the batch commit. A whole-plan refusal would lose the day's other
  knowledge as well.

## The decision

- An operation whose target page is retired is dropped from the plan before the critique,
  with its slug and the page's status printed. The rest of the plan is drafted, reviewed
  and committed as usual.
- Retired pages stay in the snapshot as sources: they are evidence of what was decided, and
  the model reading one is how it learns that the topic was settled and then replaced.
- Nothing else changes: no path, environment variable, or contract text. Rule 12 already
  says this; the compile now obeys it.
