# Calendar arithmetic

`scripts/calendar_pass.py` is qualified in place: one existing responsibility, no
production source change, new runtime path or wrapper. The module imports only
stdlib and is imported in a fresh process that refuses other product modules.
Its public functions/types and existing limits retain their original behavior.

The manifest's ownership section names the source, tests and documents this
module owns. Its complete packet also includes shared measurement/import helpers,
pytest bootstrap, required instructions and historical context. Including a file
does not transfer its ownership. Mixed integration scenarios remain separately
qualified; their assertions were not discarded.

Run `uv run pytest tests/modules/calendar_pass  -q -s`.
The manifest enforces the 100000-token owner limit on the serialized bundle.
This establishes a bounded maintenance packet, not complete repository coverage
or a claim that all product callers have been independently modularized.

Source: ../research/2026-11-02-existing-leaf-contracts.md.

`computed_gaps` uses the first two distinct valid ISO dates, or one date plus
the supplied question anchor, and returns an absolute day difference.
`states_the_gap` applies the existing one-day tolerance and week alternatives.
`calendar_note` renders data for the caller; this module does not call a model
or decide whether the evidence supports a claim.

## Canonical ownership reference

Existing stdlib-only python_parse, calendar_pass, iso_time, reply_json, page_status
and code_intelligence boundaries are qualified in place with owned context
manifests; mixed tests separate pure assertions from integration scenarios without
changing assertions. Shared test-only import guards live in tests/module_boundaries.py.
