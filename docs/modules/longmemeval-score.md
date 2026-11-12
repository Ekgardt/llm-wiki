# Deterministic LongMemEval scoring

Owns benchmark/longmemeval_score.py unchanged at its public path. Text/alias
normalization, applicability, denominators, recorded costs and refusal/twin
calibration operate on supplied rows. The only project import is longmemeval_data,
whose complete implementation and guide remain in context. No judge, retriever,
worker or provider runs here. This is not equivalence to an LLM-judge score.

Preserve Unicode ranges, article removal, substring/alias matching, token F1,
rounding, bool-only judge labels, provider/harness exclusions, None versus zero,
separate applicable denominators and existing nonfinite/error behavior. Rubrics
are not text misses; ungraded failures do not become incorrect answers. Cost is
computed from recorded prompt tokens and does not become actual billed usage.
Data-copy/alias behavior, public and private callers and output shapes remain.

Thirty-eight original pure tests now have this owner; the three cost tests remain
in tests/test_the_decision_rule_is_computed.py. The other35move into its package.
All32remaining judge/runner/vault integration tests stay in their original files.
Shared synthetic fixtures have one location. All original function/assignment
ASTs and assertions are preserved. Run this package and the seven original mixed
suites named in the qualification log through normal pytest bootstrap.

The packet retains complete source, dependency implementation, tests, instructions
and relevant historical evidence. It uses the existing exact-record format for
2026-09-19-a-number-names-its-stand.md:15records at existing topic headings,
with complete source identity, contiguous UTF-8 byte coverage and record hashes.
Selected records retain the complete introduction, refusal-calibration decision
and sources. Sections2and3 maintain published comparison tables and provenance
notices; they do not change scoring functions or input contracts. Those independent
publication records remain intact outside the scorer packet, including their
LongMemEval examples. The boundary follows responsibilities and existing headings,
not token-sized ranges. The
catalog and refusal-calibration record are owned here; other records stay visible
as unassigned or shared context. Whole-file ownership is not claimed.

## Qualification research — 2026-11-03

Fresh built-in index at a09c6011, get_architecture, callers and full source/test
reads identify real consumers;70baseline tests pass. A preliminary packet with
whole mixed suites and all histories measured104798tokens, before qualification.
Preserve the coherent existing scorer and isolate pure tests. Splitting runtime
functions arbitrarily adds coupling; omitting implementations or required evidence
would violate the maintenance contract. Topic records separate genuinely different
historical subjects without deleting or rewriting bytes. Final acceptance requires
all full packet/closure checks and <=100000 o200k_base tokens.

Sources checked: [bounded contexts](https://martinfowler.com/bliki/BoundedContext.html),
[import contracts](https://import-linter.readthedocs.io/en/stable/contract_types/),
[JSON semantics](https://docs.python.org/3.14/library/json.html).
These support boundaries/interchange, not fresh scientific validation of historical
metrics. Python3.14, dependencies, benchmark datasets and live installation stay as
previously approved. No new benchmark efficacy or full-cycle token savings claim.

## Canonical structure contract — 2026-11-03

[LongMemEval scoring](longmemeval-score.md) owns existing text metrics,
applicability/denominators, costs and refusal calibration. Model judging and report
execution remain outside. Its reviewed September19 history catalog partitions the
unchanged document by topic; only selected records enter the packet, and the
others have their own owner. No metric or public path changes.
