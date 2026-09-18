# The project context page gets its command back

Dated 2026-09-18. Finding M-C2 of the third audit: `scripts/build_context.py` (425 lines)
has no importer, hook, scheduler entry, installer call or skill, and the first round left
the decision open because it is the only writer of a documented file.

Files: `scripts/build_context.py`, `docs/USER-GUIDE.md`,
`tests/test_a_project_context_page_has_a_command.py`.

## What was found

- The module writes `knowledge/projects/<slug>/context.md`: a compact per-project brief
  built from the pages tagged with that project, the project's recent daily breadcrumbs and
  its `state.md`. It writes through `mutate_knowledge`, so it is already inside the
  transaction boundary every automatic writer must use, and
  `tests/test_automatic_writer_integration.py` lists it as one.
- That file is documented, and four readers open it: `docs/STRUCTURE.md` shows it in the
  project layout, `CLAUDE.md`'s page-type table lists `project-context`,
  `claim_tree_manifest.PROJECT_CLAIM_FILES` and `corpus_snapshot.PROJECT_FILES` include it,
  and `lint_memory` checks it. Deleting the only writer of a file four readers expect, and
  a page type the contract names, is not a clean-up.
- What is actually missing is the one thing that makes a CLI module reachable: it is named
  in no README, user guide, skill, or scheduler. `scripts/build_context.py --slug <name>
  --write` has worked all along; nobody reading the documentation could learn that.
- It is not a candidate for the nightly or weekly pass: those passes are for work nobody is
  waiting for, and a per-project brief is written when a person starts working on that
  project. Adding a new automatic writer of the knowledge zone would also be a change to
  what the product does on its own, which is the owner's decision, not a clean-up either.

## Practice on this date

- "Yagni … the cost of carry: the code for the presumptive feature adds some complexity to
  the software, this complexity makes it harder to modify and debug that software"
  ([Martin Fowler, Yagni](https://martinfowler.com/bliki/Yagni.html)). The cost of carry is
  real, but the test of Yagni is whether the feature is presumptive. This one is not: its
  output is named in the layout, in the page-type table, and in four readers.
- The owner's standing rule in this round's brief resolves the case directly: a feature
  nothing runs is either wired in — when a contract promises it and the wiring is small —
  or deleted with its tests and its contract sentence. Here the contract promises the file
  and the wiring is one documented command.

## The decision

- Keep `build_context.py`, and name it in `docs/USER-GUIDE.md` beside the other manual
  commands, so the documented page type has a documented way to be written. A test asserts
  the two stay together: if the module is ever deleted, the guide line must go with it, and
  if the guide stops naming it, the module is dead again and this decision is void.
- No scheduled caller is added; no path, environment variable or contract text changes.
