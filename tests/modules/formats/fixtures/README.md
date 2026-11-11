# Frozen receipt contract data

Validated on 2026-11-02 using the original `compile_memory.py` and
`reliable_memory.py` from candidate commit `3e1def7e`, loaded in an isolated process
with temporary runtime state. The `.json` files are the complete parser results.

`v3.md` is the exact `files["compile-receipt.md"]` from the existing public synthetic
`tests/fixtures/compile-receipt-v3-bag.json`; no private vault content is included.
`v4.md` adapts that descriptor to whole-source byte bounds, recalculates identity,
manifest and operation digests, and passes the original v4 parser. `v2.md` is a
synthetic historical empty-operation receipt accepted by the original v2 parser.
These fixtures assert syntax and identity only, never transaction commitment.
