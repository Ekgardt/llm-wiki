# A filed answer is counted in the log, not named

Dated 2026-09-14. The automatic-writer part of item 1.3 of
`docs/AUDIT-2026-09-14-2.md`. The research before the fix.

## What was found

- `knowledge/log.md` is tracked in the public repository, and the runtime appends to
  it. The compile pass already obeys the publication rule: `_touched_phrase` names
  only pages `.gitignore` publishes and counts the rest
  (`rebuild_memory_index.published_paths`), because "a private page's slug is itself
  personal content".
- `query_memory.main --file-back` does not. It appends
  ``Filed Q&A `knowledge/notes/<slug>.md` `` where the slug is made from the
  question's own words (`slugify(question)`). Every note is unpublished since
  2026-09-10, so the question's text goes into the tracked file.
- The guard `tests/test_structure.py::test_the_vault_index_and_log_name_only_published_notes`
  looks only for `[[knowledge/notes/…]]` links and misses the back-quoted path.
- The code graph: `append_log` has one caller, `query_memory.main`; `file_back` is
  listed as an automatic writer in `tests/test_automatic_writer_integration.py`.
- Most of the 1 696 uncommitted lines in the main checkout's `log.md` are not from
  either writer: 64 are compile lines (already filtered), the rest is prose agents were
  told to append (section 3 of `CLAUDE.md`, rule 4, and four skills). No filter can
  make free prose publishable. Taking the file out of git is a change to the
  repository's documented structure (`CLAUDE.md` section 2 names it tracked) and
  needs the owner's decision; it is not made here.

## Practice on this date

- The rule this repository already states for the file: publish a page's name only
  when the page itself is published (the docstring of
  `rebuild_memory_index.published_paths`, and `CLAUDE.md` section 2, "What keeps
  private knowledge out of a public repository is `.gitignore`"); one writer bypassing
  it is a defect of that writer, not a new policy.
- Git ignores changes to a tracked file only through per-clone index flags
  (`git update-index --skip-worktree`), which do not travel with the repository
  ([git-update-index](https://git-scm.com/docs/git-update-index#_skip_worktree_bit)) —
  why a code-side filter cannot replace a structural decision about the file.

## The decision

- `query_memory.main` names the filed page through `published_paths`: a published
  page by path, an unpublished one as `1 unpublished page`.
- The structure guard also reads back-quoted `knowledge/notes/…` paths.
- Reported to the owner as a decision: stop tracking `knowledge/log.md` (ship a
  template, keep the runtime file private), or keep it tracked and forbid free-text
  entries.

Files: `scripts/query_memory.py`, `tests/test_structure.py`,
`tests/test_a_filed_answer_is_counted_not_named.py`,
`docs/research/2026-09-14-a-filed-answer-is-counted-not-named.md`.
