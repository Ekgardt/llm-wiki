# Navigation value types

`scripts/code_intelligence.py` is qualified in place: one existing responsibility, no
production source change, new runtime path or wrapper. The module imports only
stdlib and is imported in a fresh process that refuses other product modules.
Its public functions/types and existing limits retain their original behavior.

The manifest's ownership section names the source, tests and documents this
module owns. Its complete packet also includes shared measurement/import helpers,
pytest bootstrap, required instructions and historical context. Including a file
does not transfer its ownership. Mixed integration scenarios remain separately
qualified; their assertions were not discarded.

Run `uv run pytest tests/modules/code_intelligence tests/test_code_intelligence.py -q -s`.
The manifest enforces the 100000-token owner limit on the serialized bundle.
This establishes a bounded maintenance packet, not complete repository coverage
or a claim that all product callers have been independently modularized.

Source: ../research/2026-11-02-existing-leaf-contracts.md.

Capability, PositionEncoding and DiagnosticSeverity keep their existing enum
values. PositionRange is frozen/slotted and accepts nonnegative signed-int64
byte boundaries with end >= start, including an empty range. Booleans are not
integers for this contract. It represents byte offsets, not UTF-16 positions
or a claim that a range was verified against a source file.

## Canonical ownership reference

Existing stdlib-only python_parse, calendar_pass, iso_time, reply_json, page_status
and code_intelligence boundaries are qualified in place with owned context
manifests; mixed tests separate pure assertions from integration scenarios without
changing assertions. Shared test-only import guards live in tests/module_boundaries.py.
