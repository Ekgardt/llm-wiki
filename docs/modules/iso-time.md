# ISO timestamp normalization

`scripts/iso_time.py` is qualified in place: one existing responsibility, no
production source change, new runtime path or wrapper. The module imports only
stdlib and is imported in a fresh process that refuses other product modules.
Its public functions/types and existing limits retain their original behavior.

The packet owns its source, the tests named in the manifest, its own boundary and
budget guards, shared measurement/import helpers, pytest bootstrap, required
instructions and the linked historical rationale. Mixed integration scenarios
remain separately qualified; their assertions were not discarded.

Run `uv run pytest tests/modules/iso_time  -q -s`.
The manifest enforces the 100000-token owner limit on the serialized bundle.
This establishes a bounded maintenance packet, not complete repository coverage
or a claim that all product callers have been independently modularized.

Source: ../research/2026-11-02-existing-leaf-contracts.md.

`parse_instant` supports the existing Z/fraction normalization on Python 3.10;
unzoned parsed instants are UTC. `utc_text` emits fixed-width UTC microseconds.
`local_now` and `block_instant` deliberately use the machine's local clock;
`names_block` checks the existing quarter-hour offset rule for a supplied UTC
instant. These distinct time contracts are preserved, not unified implicitly.
