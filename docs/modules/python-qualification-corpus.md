# Deterministic Python qualification corpus

Owns benchmark/generate_python_qualification.py in place, byte-identical.
It writes the synthetic corpus and optional gold JSON to caller-supplied paths.
Git initialization, Pyright startup, provider qualification, performance sampling,
model calls and navigation acceptance gates remain runner responsibilities.

Preserve exactly100000physical lines,48Python files,32padding modules,seed411,
400gold queries and50mutation cycles. Keep source bytes, canonical JSON, all
source/gold/workload hashes, complete mutation fields, source ranges, UTF-8 byte
versus codepoint positions, Unicode symbols, broken-file cases and error behavior.
The fixture's pythonVersion3.10 is pinned test data, not the project runtime;
execution uses the approvedPython3.14 series. No corpus output is rebaselined.

Six pure tests and their helpers move with unchanged function/decorator ASTs.
The module-scoped generated_pair fixture, one byte-offset helper and the shared
capability tuple have one support location imported by retained integrations.
The other151cases remain in tests/test_code_navigation_benchmark.py, including
padding/performance binding, canonical schema validation and real runner behavior.
A test whose helper calls the runner is not moved into this pure packet. No original
assertion is deleted or rewritten. New tests exercise actual CLI output, the gold
file against pinned digests and refusal of an existing destination without damage.

Full July22implementation-plan and September11complexity history remain shared
context. Historical versions, thresholds and installation claims remain dated
records; no historical plan is newly executed by this qualification. The current
source and pinned benchmark manifest retain the generator's exact contract.
The navigation userguide and broader design spec concern consumers and add no
separate generator behavior to the included full plan; each has its own owner.
No record clipping or source omission is used.

Run tests/modules/python_qualification_corpus and the complete retained benchmark
suite with normal pytest bootstrap. Verify actualCCN/shape, Ruff, whole-tree
collection, unchanged generator bytes and exact original test/helper transfer.
The full packet, including all test support/instructions/history, must fit<=100000
o200k_base tokens and reconstruct every original byte. This proves corpus
maintenance isolation, not navigation quality, full-project completion or token
savings over a real maintenance task. No dependency change or live installation.

## Qualification research — 2026-11-03

Fresh indexa24ce595,generation-18e4a7f6f5db0f75-3cb1131d covers2330sources.
Architecture/callers and full source inspection identify19generator callers and
preserve the runner-facing data types. Six selected baselinecases pass. Keep the
cohesive deterministic generator; merging its runner adds over1million source-only
tokens, while helper-level splitting adds no useful boundary.

Primary sources checked: [bounded contexts](https://martinfowler.com/bliki/BoundedContext.html),
[import contracts](https://import-linter.readthedocs.io/en/stable/contract_types/),
[temporary-file verification](https://docs.pytest.org/en/stable/how-to/tmp_path.html).
These support responsibility/import/IO checks, not historical benchmark scores.

## Canonical structure contract — 2026-11-03

The [deterministic corpus generator](python-qualification-corpus.md) owns
fixture creation and gold bytes. Navigation execution, sampling and acceptance
gates remain with the runner; original integration assertions stay intact.
