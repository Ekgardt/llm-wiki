# Safe Python parsing

`scripts/python_parse.py` is qualified in place: one existing responsibility, no
production source change, new runtime path or wrapper. The module imports only
stdlib and is imported in a fresh process that refuses other product modules.
Its public functions/types and existing limits retain their original behavior.

The packet owns its source, the tests named in the manifest, its own boundary and
budget guards, shared measurement/import helpers, pytest bootstrap, required
instructions and the linked historical rationale. Mixed integration scenarios
remain separately qualified; their assertions were not discarded.

Run `uv run pytest tests/modules/python_parse tests/test_python_3_10_does_not_crash_on_a_deep_expression.py -q -s`.
The manifest enforces the 100000-token owner limit on the serialized bundle.
This establishes a bounded maintenance packet, not complete repository coverage
or a claim that all product callers have been independently modularized.

Source: ../research/2026-11-02-existing-leaf-contracts.md.

`parse_python` returns the ordinary AST and preserves `PARSE_FAILURES`. On Python
3.10 the existing operator-chain preflight prevents a stack-killing expression;
flat literals remain parseable. Newer Python uses its parser's own guard. The
3000-operator safety boundary and small-stack regression are preserved.
