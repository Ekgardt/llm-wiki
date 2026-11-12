# LoCoMo question adapter

Owns benchmark/locomo_data.py at its unchanged public path and the complete
original tests/test_locomo_becomes_a_question_this_stand_can_read.py.
Local dataset loading, conversion and JSON publication form one responsibility.
No model, retrieval worker or network operation is imported or started.

Preserve first-speaker roles, image captions, numeric session order, evidence labels, category5 empty answers and _abs suffix; the question follows the latest session.
Keep existing cache defaults, field types, aliasing, UTF-8, malformed-input error
propagation and missing-dataset refusal. Synthetic fixtures are test data only;
production never invents a replacement dataset. This is compatibility
qualification, not a new dataset validation or benchmark effectiveness result.

Run the original test file and tests/modules/locomo_data. Added temporary-file
tests exercise actual reading, conversion, nested destination creation and JSON
serialization, including Unicode, empty and malformed inputs. Source and every
original assertion remain unchanged. Import guards reject runtime dependencies;
full packets retain real longmemeval_data source and its guide, bootstrap and
instructions, with <=100000 o200k_base tokens and exact reconstruction.

The full historical record ../research/2026-09-06-what-locomo-is-worth-measuring-on.md is
included as context. It retains dated experimental and dataset claims; they are
not revalidated here. Shared preparation history remains with its original owners.

## Qualification research — 2026-11-03

Fresh built-in index7673d57b, get_architecture, caller paths and full source/tests
identify the CLI, pure conversion and generic downstream question interchange.
Four-adapter baseline30passes; these three JSON adapters contribute23. Separate
owners preserve dataset-specific rules; a combined adapter or one module per
helper would introduce coupling or needless fragmentation. Python3.14 unchanged.

Sources checked: [bounded contexts](https://martinfowler.com/bliki/BoundedContext.html),
[import contracts](https://import-linter.readthedocs.io/en/stable/contract_types/),
[JSON semantics](https://docs.python.org/3.14/library/json.html).
They support responsibilities and interchange checks, not new empirical claims.

## Canonical structure contract — 2026-11-03

[LoCoMo](locomo-data.md), [RefusalBench](refusalbench-data.md)
and [LIT-RAGBench](litragbench-data.md) each own their existing local
loader/converter and original tests. Full real preparation dependencies remain in
their packets. Public paths, source bytes, dates and normalization rules stay
unchanged; no corpus download or model execution is introduced.
