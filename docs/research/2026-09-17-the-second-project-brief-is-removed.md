# The second project brief is removed

Dated 2026-09-17. The audit's dead-code row `scripts/build_context.py` and the `context.md` it
writes, left to the owner and delegated back.

## What was found

- `build_context.py` reads every knowledge page tagged with a project, the project's `state.md`
  handoff, the recent daily-log breadcrumbs for that slug and the last heartbeat, and packs them
  into a per-project brief. With `--write` it commits that brief to
  `knowledge/projects/<slug>/context.md` through the Markdown transaction.
- Nothing runs it. Not the hooks, not the adapter, not the nightly or weekly schedules, not the
  installer, not the MCP server, not the OpenCode plugin. Its only references in the repository
  are tests, one of which pins it in the list of automatic write points.
- The live per-project brief is a different mechanism: the adapter appends
  `recover_project_handoff(store, slug, …)` to the SessionStart context, projected from the
  append-only `journal.md`, and the rest of the block comes from
  `session_start_context.build_context_items`. So the product has two implementations of one idea
  and runs the other one.
- `context.md` is also a name several readers know: `search_memory`, `build_tiers`,
  `contextual_retrieval`, `reflection` and `impact_analysis` skip it, `claim_tree_manifest` and
  `claims` read it as a project claim page, and `corpus_snapshot` lists it among the project files.
- The OKF type is not this file's alone: `corpus_snapshot._project_source_type` types every project
  page that is not `state.md` as `project-context`, `journal.md` included. So the type, its row in
  the page-type table and the readers' handling of it all stand on their own.

## Practice on this date

- The owner's rule for this audit is that a feature nothing runs is either wired in, when a
  contract promises it and the wiring is small, or deleted. Wiring this one in is neither small
  nor free: it would write a Markdown transaction per session start and inject a second brief into
  a block that already has a hard character ceiling it has to drop sections to meet — against the
  token-economy rule, and duplicating the handoff the journal already projects.
- Nothing is deleted that an outside caller may use: this script is not a hook, not a scheduler
  target, and is named in no README or user guide.

## The decision

- `scripts/build_context.py` is deleted, with the tests that only exercise it.
- The `project-context` OKF type stays: it types `journal.md` and any other project page that is
  not `state.md`, and its row in the page-type table stays true.
- The readers that know the name `context.md` keep it. An installed vault may hold a file written
  by an earlier manual run, and those entries are what keep such a file out of retrieval and
  tiering. Removing them would suddenly index a stale auto-generated page.
- `docs/STRUCTURE.md` stops listing `context.md` as part of what a project directory contains, and
  names it as what it now is: a legacy file some vaults still hold.

Files: `scripts/build_context.py`, `docs/STRUCTURE.md`,
`tests/test_automatic_writer_integration.py`, `tests/test_context_noise.py`,
`tests/test_context_compiler.py`, `tests/test_security_invariants.py`
