# Two small benchmark files under the gate

Date: 2026-09-11. Trigger: audit finding L13, the two smallest entries:
`benchmark/generate_python_qualification.py::_with_padding` (CCN 12, one
function that lays out padding modules, their blocks and the user module)
and `benchmark/run_contradiction_benchmark.py::build_corpus` (CCN 10, a
category `if/elif` ladder inside a double loop) and `main` (10).

## Sources

1. `tests/test_python_qualification*.py` and the contradiction benchmark
   tests: the generated fixture bytes and the corpus JSON are compared
   against checked-in digests, so the output must stay byte-identical.
2. Rule 5's remedy: a ladder over category names becomes a table of
   builders; a loop body that writes one module becomes one function.

## Decision

Output unchanged byte for byte. `_with_padding` emits each block and
module through `_padding_block` / `_padding_module`, the user module
through `_padding_users`, and checks the totals in
`_require_padding_exact`; the operation counter threads through in the
same order. `build_corpus` builds a `_CaseSpec` per case from one
builder per category, renders the source once, and maps each spec to its
case dictionary. `main` reads the corpus and judges the gates through
two named helpers.

Files: `benchmark/generate_python_qualification.py`,
`benchmark/run_contradiction_benchmark.py`, `CHANGELOG.md`,
`docs/AUDIT-2026-09-10-memory-and-retrieval.md`.
