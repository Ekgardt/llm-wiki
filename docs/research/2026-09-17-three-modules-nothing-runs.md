# Three modules nothing runs

Date: 2026-09-17

Files: `scripts/loop_detector.py`, `scripts/agent_timeline.py`,
`scripts/tool_breadcrumb_append.py`, `tests/test_audit_runtime_contracts.py`,
`tests/test_automatic_writer_integration.py`, `tests/test_compile_transactions.py`,
`tests/test_plugin_helpers.py`, `tests/README.md`

## What was found

The third audit listed four modules with no importer, no hook, no scheduler entry,
no installer call, no skill and no README or user-guide command. Each was checked
again, by the code graph and by `git grep` over every tracked file except the dated
history (`docs/research/`, `docs/superpowers/`, `CHANGELOG.md`, benchmark result JSON).

- `scripts/loop_detector.py` — a hand-run report of repeated edits and errors. The
  graph shows inbound edges from tests only. Nothing names it in `scripts/`,
  `integrations/`, `skills/`, `rules/`, `.github/`, `install.sh`, `install.ps1`, the
  READMEs, the user guide, `docs/STRUCTURE.md` or `CLAUDE.md`.
- `scripts/agent_timeline.py` — imported only by `loop_detector.py`. It falls with it.
- `scripts/tool_breadcrumb_append.py` — its docstring calls it a helper of the OpenCode
  plugin, but `scripts/llm-wiki-memory-opencode.js` starts only `integration_adapter.py`
  and `graph_hint.py`. Tool events reach the daily log through the lifecycle adapter.
- `scripts/build_context.py` — also has no caller. But it is the only writer of
  `knowledge/projects/<slug>/context.md`, a file `docs/STRUCTURE.md` names in the project
  layout, the page-type table lists as `project-context`, and the claim readers
  (`claim_tree_manifest.py`, `corpus_snapshot.py`, `lint_memory.py`, `claims.py`) still
  open. Removing the only writer of a documented file is a contract question, not a
  clean-up.

One test used a dead module to check a live behaviour:
`test_compile_page_preserves_per_agent_evidence_attribution` compiled a page from three
daily blocks and asked `agent_timeline` who wrote them. The product fact under it — every
evidence reference in a compiled page resolves to its own source block — is what lint and
the MCP page reader rely on, through `extract_evidence_references` and `EvidenceResolver`.

## Source

Martin Fowler, "Yagni", https://martinfowler.com/bliki/Yagni.html (fetched 2026-09-17),
on the cost of carry: "The code for the presumptive feature adds some complexity to the
software, this complexity makes it harder to modify and debug that software, thus
increasing the cost of other features."

Here the cost is concrete: the writer-entry-point matrix, the plugin-helper suite and the
audit-contract suite all carried rows for code no user path reaches, and every rule-5
refactor had to be applied to them as well.

## Decision

- Delete `loop_detector.py`, `agent_timeline.py` and `tool_breadcrumb_append.py`, the tests
  that test only them, and their rows in the writer-entry-point matrix.
- Keep the compile attribution test, and make it ask the product's own resolver: the
  references extracted from the compiled page must resolve to the three observations.
- Leave `build_context.py` in place. Whether the project `context.md` file stays a
  documented part of the layout is the owner's decision.
- Dated history (research notes, plans, the changelog, benchmark result files, the
  2026-08-14 audit status) keeps its mentions: it records what existed then.
