# Safe Python parsing

`scripts/python_parse.py` is qualified in place: one existing responsibility, no
new runtime path or wrapper. The module imports only
stdlib and is imported in a fresh process that refuses other product modules.
Its public parse function and failure types retain their behavior.

The packet owns its source, the tests named in the manifest, its own boundary and
budget guards, shared measurement/import helpers, pytest bootstrap, required
instructions and the linked historical rationale. Mixed integration scenarios
remain separately qualified; their assertions were not discarded.

Run `uv run pytest tests/modules/python_parse tests/test_python_3_10_does_not_crash_on_a_deep_expression.py -q -s`.
The manifest enforces the 100000-token owner limit on the serialized bundle.
This establishes a bounded maintenance packet, not complete repository coverage
or a claim that all product callers have been independently modularized.

Source: ../research/2026-11-02-existing-leaf-contracts.md.

`parse_python` returns the ordinary AST and preserves `PARSE_FAILURES`. The
approved Python 3.14 runtime provides its own compiler depth guard. The obsolete
3.10 operator-chain preflight and its constants are removed; small-stack and flat
literal regression scenarios remain, exercising the real parser.

## Canonical ownership reference

Existing stdlib-only python_parse, calendar_pass, iso_time, reply_json, page_status
and code_intelligence boundaries are qualified in place with owned context
manifests; mixed tests separate pure assertions from integration scenarios without
changing assertions. Shared test-only import guards live in tests/module_boundaries.py.
