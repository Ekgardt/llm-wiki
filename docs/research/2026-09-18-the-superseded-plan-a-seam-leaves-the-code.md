# The superseded Plan A seam leaves the code

Dated 2026-09-18. Third audit, section 4.2 and 4.4: the sealed-workspace island in
`scripts/code_workspace.py`, the `verify_native_analysis` / `closed_world` island in
`scripts/code_intelligence.py`, `evidence_graph.database_closed_world`, and the
`evidence-graph/v3` schema that only tests build. The research before the removal.

Files: `scripts/code_workspace.py` (removed), `tests/test_code_workspace.py` (removed),
`scripts/code_intelligence.py`, `scripts/evidence_graph.py`,
`scripts/evidence_graph_builder.py`, `scripts/generation_catalog.py`,
`scripts/corpus_snapshot.py`, `tests/code_kernel_helpers.py`,
`tests/test_evidence_graph.py`, `tests/test_evidence_graph_builder.py`,
`tests/test_generation_catalog.py`, `CLAUDE.md`, `AGENTS.md`.

## What was found

- `CLAUDE.md` states the position already: the 2026-07-21 Plan A's "one-shot consent/SCIP/
  publication Tasks 6-16 are superseded", and the replacement plan is the read-only LSP
  navigation path that is implemented and running. What it also says — "Foundation Tasks 1-5
  of the 2026-07-21 Plan A remain implemented" — is true only in the sense that the code is
  still in the repository.
- `docs/STRUCTURE.md`'s implemented checkpoint names `evidence-graph/v2`, and
  `tests/test_structure.py` asserts that `evidence-graph/v3` does **not** appear in it. The
  published contract has no v3 in it.
- Nothing in production builds a v3 generation. `graph_schema=` is never passed outside
  tests; the five files that build `evidence-graph/v3` are test files. A v3 manifest
  requires `code_capture`, and `code_capture` is produced only by
  `code_workspace.collect_repository_code`, which no production module calls — the only
  production references are `code_capture_as_dict` / `validate_code_capture`, reached solely
  to validate a contract a caller supplied, and no caller supplies one
  (`doctor`, `repository_index`, `sync_memory`, `scheduled_nightly` pass none).
- Checked on the installed vault: none of the 15 generation manifests under
  `cache/evidence-graph/generations/` carries a `code_capture` key, so removing the optional
  manifest section cannot make an existing generation unreadable.
- The parts are one unit, not four. The sealed workspace is the only producer of a valid
  `CodeCaptureContract` (it computes the membership digest the catalog re-checks against the
  canonical source manifest), `verify_native_analysis` is the only constructor of a
  `VerifiedAnalysisBatch`, and a v3 database requires both. Removing any one of them alone
  would leave a schema nothing can fill, or force a test to recompute a membership digest by
  copying the product's own logic.

## Practice on this date

- The repository's own rule for dead code this round, from the owner: delete what nothing
  needs, and above all break nothing. Its counterweight is the audit's own constraint: SQL
  columns sit in a published generation schema, so their removal is a schema decision, which
  is why this page exists and is cited from the commit.
- SQLite `user_version` is what distinguishes the two schemas here
  (https://www.sqlite.org/pragma.html#pragma_user_version, fetched 2026-09-18): a v2
  database records 2 and a v3 database 3. Removing the v3 branch does not rewrite any
  existing database — it removes the ability to create and validate one, and every artifact
  on disk is v2.

## The decision

Remove the whole seam:

1. `scripts/code_workspace.py` and `tests/test_code_workspace.py` go.
2. `code_capture` leaves the corpus snapshot (`CodeCaptureFile`, `CodeCaptureContract`), the
   builder's parameters, and the generation manifest (`_OPTIONAL_MANIFEST_SECTIONS`, the
   membership validation, the v3 requirement).
3. `GraphSchema.V3` and everything keyed to it leaves `evidence_graph.py`: the extension
   schema, its tables and indexes, the batch rows, the publication columns, the v3 branches
   of validation, and `database_closed_world`, which nothing referenced at all.
4. `verify_native_analysis`, `closed_world` and their helpers leave
   `scripts/code_intelligence.py`. The module keeps the types production uses —
   `Capability`, `PositionEncoding`, `PositionRange`, `AnalysisIdentity` and the claim
   records — because `code_navigation`, `lsp_positions`, `pyright_session`, `mcp_server`
   and `evidence_graph` read them.
5. `CLAUDE.md` and `AGENTS.md` stop saying Foundation Tasks 1-5 remain implemented, and say
   instead what is true: the corpus generation is `evidence-graph/v2`, and the whole
   2026-07-21 Plan A — its foundation included — is superseded by the read-only LSP
   navigation path.

What stays: every v2 artifact, every reader, the corpus snapshot, the catalog contract, and
the navigation path. No generation on disk changes, and no migration is needed.
