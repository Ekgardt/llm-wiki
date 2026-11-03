# Offline ledger counting stand

Owns benchmark/ledger_offline.py and the complete original
tests/test_the_offline_ledger_reads_the_question_not_the_label.py in place.
Both remain byte-identical. The stand reads supplied judged JSONL and per-question
staging JSON, derives records deterministically from user mentions, counts through
the real ledger core, and prints/writes verdict and reconciliation summaries.
Question wording opens counting; benchmark labels are read for scoring only.

Keep exact number/year parsing, mention phrases, quantity precedence, date-window
resolution, question-id split, abstention filtering, verdicts, UTF-8 IO and CLI
arguments. Malformed or missing files remain errors. This is a local trusted-file
benchmark, not a sandbox for untrusted question ids. No model-written records are
produced and no real benchmark accuracy is newly measured. Existing in-memory
SQLite connection lifetime behavior is unchanged.

The complete packet includes real ledger_core, aggregation_pass, temporal_anchor
and reply_json source, including lazy imports. Their ownership stays separate.
Shared September22 ledger and September19 aggregation histories remain complete;
historical statistical and installation claims are not current qualification.
No source omission, contract substitution, new dependency or runtime path.

Run the original five tests and tests/modules/ledger_offline with normal pytest
bootstrap. New checks cover explicit imports, fresh import isolation, real temporary
judged/staging/output files, malformed input and missing question data. The complete
packet must fit <=100000 o200k_base tokens and reconstruct every path/byte exactly.
Do not infer whole-project completion or useful full-cycle token savings from size.

## Qualification research — 2026-11-03

Built-in index at7eab3a62, generation-18e4a7241779680d-3fb30704, covers2326sources.
Architecture/callers and complete source searches confirm the standalone benchmark
entry and product dependencies. Baseline five tests pass. Keep the existing cohesive
stand; merging runtime execution broadens dependencies, while helper-level splitting
adds unnecessary interfaces. The approved decomposition plan covers this boundary.

Primary sources checked: [bounded contexts](https://martinfowler.com/bliki/BoundedContext.html),
[import contracts](https://import-linter.readthedocs.io/en/stable/contract_types/),
[temporary-file tests](https://docs.pytest.org/en/stable/how-to/tmp_path.html).
These support boundary and IO verification, not the historical benchmark outcomes.
