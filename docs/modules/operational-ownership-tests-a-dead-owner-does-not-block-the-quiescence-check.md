# Tests of `scripts/operational_ownership.py`

- `tests/test_a_dead_owner_does_not_block_the_quiescence_check.py`: The deletion check and the backup wait for live owners, not for dead ones.

`scripts/operational_ownership.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_dead_owner_does_not_block_the_quiescence_check.py -q`.
