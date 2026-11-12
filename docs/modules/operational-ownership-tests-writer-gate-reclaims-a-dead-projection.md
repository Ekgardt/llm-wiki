# Tests of `scripts/operational_ownership.py`

- `tests/test_writer_gate_reclaims_a_dead_projection.py`: A dead writer's gate row is reclaimed, not a permanent wedge.

`scripts/operational_ownership.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_writer_gate_reclaims_a_dead_projection.py -q`.
