# Three islands nothing sails to, and the one that cannot be sunk yet

Date: 2026-09-17. Audit 3, code intelligence, findings C1, C2, C3 and the C7
leftovers of the graph area.

Files: `scripts/code_extractor.py`, `scripts/code_graph.py`,
`scripts/code_intelligence.py`, `scripts/retrieval.py` (one entry),
`tests/test_code_graph.py`, `tests/test_code_extractor.py`,
`tests/test_code_intelligence.py`

## What was found

### C2 — SCIP in the extractor

`extract_code(..., scip_symbols=...)` accepts compiler-minted symbols and, when
one covers a declaration's name span exactly, files that node under the
identity scheme `scip/v1` instead of `code-symbol/v1`. The only production
caller is `doctor._code_partitions` (`scripts/doctor.py:7678`) and it passes
`repository_id`, `deadline` and `cancelled` and nothing else. Nothing in
`scripts/`, `benchmark/` or `integrations/` ever builds a `ScipSymbol`.
`identity_scheme` carries no CHECK constraint in the generation schema
(`scripts/evidence_graph.py:252`), so removing a scheme no producer emits
changes no table.

### C3 — co-change

`code_graph.analyze_co_changes` and `refine_call_edges_with_co_changes`
(~290 lines with their helpers) and `code_extractor`'s `CoChange` /
`co_changes=` / `_add_co_changes` are reachable only from tests.
`CO_CHANGED_WITH` therefore has no producer, while `retrieval.py` still carries
the weight `"CO_CHANGED_WITH": 0.45` in `GRAPH_EDGE_DECAY` and, through
`tuple(GRAPH_EDGE_DECAY)`, in the `GLOBAL` expansion set.

Measured here on 2026-09-17, `analyze_co_changes(Path("."), timeout=60)` over
this repository's last 2,000 commits: **3.78 s, 191 edges**. Top rows are
`README.ru.md ↔ README.zh-CN.md` (115 shared commits) and
`scripts/<x>.py ↔ tests/test_<x>.py`.

The obstacle to wiring it is not the cost, it is the evidence model. Every
assertion in a generation is bound to a byte span of a captured source — that
is what `add_assertion` and the `evidence` table require, and it is why
`CoChange` carries `evidence_source_id`, `byte_start` and `byte_end` at all.
"These two files changed together in 115 commits" is a property of the *history*
and has no span in any file. `analyze_co_changes` accordingly returns no span,
and no honest adapter can invent one.

### C1 — the Plan-A consent/SCIP remnant

The audit lists `AnalysisRun`, `NormalizedAnalysis`, `AnalyzerReceipt`,
`SymbolClaim`, `RelationshipClaim`, `Validity`, `verify_native_analysis`,
`closed_world`, the `"precise"` mode, `_PROTOCOLS` and the
`consent_grant_id`/`consent_revision`/`lease_id` fields as ~1,000 dead lines.
Checked one by one, that is true of exactly one of them:

- `scripts/evidence_graph.py:679-1010` writes the `analyzer_run`,
  `analysis_scope`, `coverage`, `symbol_claim`, `relationship_claim`,
  `diagnostic`, `slice_activation` and `validity` tables from
  `batch.analysis.*`, and reads `run.analysis_mode`, `run.receipt_sha256`,
  `run.consent_grant_id`, `run.consent_revision` and `run.lease_id` directly
  (lines 755, 780-784). The table's own CHECK constraints (320, 360-367) name
  `'precise'`, `'native-syntax'` and every consent column, and `protocol` is
  checked against `('scip','lsp','native')` at 338.
- `AnalysisIdentity` is used on the **read** path, at
  `scripts/evidence_graph.py:2320-2323`, to rebuild an `analyzer_run` row of an
  existing generation.
- `verify_native_analysis` is the only minter of `VerifiedAnalysisBatch`, which
  `evidence_graph` and `evidence_graph_builder` both import and type-check.

So the cluster is unreachable in production only because nothing supplies
`verified_analyses=` — not because the code is unused by shipped code. Removing
it is a change to the published generation schema and to two files this area
does not own.

`closed_world` is the exception: it is a pure predicate over `AnalysisScope`
and `Coverage`, it writes nothing, and it has zero callers anywhere outside
`tests/test_code_intelligence.py`. The `closed_world_eligible` column is a
field of `Coverage`, set by whoever builds the row, and does not go through
this function.

### C7 leftovers

- `code_extractor.extract_sources` and `code_graph._stored_dead_candidate`
  (singular) are already gone from this branch — removed in the first fix round.
  `_stored_dead_candidates` (plural) is live and used.
- `enrich_python_semantics` building a `jedi.Project` per file: measured on this
  interpreter, 200 `jedi.Project(path=…)` constructions cost **0.0004 s** in
  total (2 µs each), against **1.26 s** for 200 `jedi.Script(...)` constructions
  of one file (6.3 ms each). The Project is 0.03% of the per-file Jedi cost.
  Hoisting it would buy nothing measurable and would introduce a module-level
  cache where there is none.

## Sources

- Measurements above, run on 2026-09-17 in this worktree on the project
  interpreter (Python 3.12, `jedi` from `.venv`).
- `CLAUDE.md`, "Implemented code-navigation slice": "Foundation Tasks 1-5 of the
  2026-07-21 Plan A remain implemented, but its one-shot consent/SCIP/publication
  Tasks 6-16 are superseded."
- `CLAUDE.md`, "Derived evidence generations": "Markdown, Git, and project
  journals are authoritative. All graph, FTS, vector, tier, and telemetry
  generation state is disposable and derived." Git history is authority, so a
  question answerable from `git log` on demand does not need to be frozen into a
  generation that is rebuilt nightly.
- The round-2 brief's standing rule from the owner: "всё что ненужно и
  бесполезно удаляй, главное ничего не сломай" — and its exception, never delete
  something an outside caller may use.

## Decision

1. **C2: delete the SCIP island from the extractor.** `ScipSymbol`,
   `SCIP_DEFINITION_ROLE`, `_defines_span`, `max_scip_symbols`, the
   `scip_symbols=` parameter, `_require_valid_symbols` and its three validators,
   and the `scip/v1` branch of `symbol_identity`, with their tests. Every node
   is then `code-symbol/v1`, which is what every node already is. `doctor.py`
   is not touched: it never passed the argument.
2. **C3: delete the co-change island.** `analyze_co_changes`,
   `refine_call_edges_with_co_changes` and their helpers in `code_graph`;
   `CoChange`, `co_changes=`, `max_co_changes`, `_require_valid_co_changes` and
   `_add_co_changes` in `code_extractor`; the `"CO_CHANGED_WITH": 0.45` entry in
   `retrieval.GRAPH_EDGE_DECAY` (one line, in another area's file, named in the
   report). Wiring was rejected: the relationship has no source span, and the
   evidence model has no place to hang an assertion without one. The question it
   answers stays answerable — by `git log` — it simply is not a generation fact.
3. **C1: delete `closed_world` and its three helpers with their tests; keep the
   rest and say why.** The verification cluster is required by the published
   generation writer and by the read path that rebuilds `analyzer_run` rows.
   Deleting it is a schema version change in `evidence_graph.py` plus the
   `verified_analyses=` parameter in `evidence_graph_builder.py` — one coherent
   change belonging to the generations area, not three files edited from here.
4. **C7: the two dead names are already removed; the Jedi hoist is not a defect**
   and is left alone, with the measurement above as the evidence.
