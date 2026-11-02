# Navigation value types

`scripts/code_intelligence.py` is qualified in place: one existing responsibility, no
production source change, new runtime path or wrapper. The module imports only
stdlib and is imported in a fresh process that refuses other product modules.
Its public functions/types and existing limits retain their original behavior.

The packet owns its source, the tests named in the manifest, its own boundary and
budget guards, shared measurement/import helpers, pytest bootstrap, required
instructions and the linked historical rationale. Mixed integration scenarios
remain separately qualified; their assertions were not discarded.

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
