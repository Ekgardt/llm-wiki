# Tests of `scripts/operational_ownership.py`

- `tests/test_a_nested_gate_waits_as_long_as_it_was_told.py`: A writer gate entered with an owner waits for a live writer, as the plain gate does.

`scripts/operational_ownership.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_nested_gate_waits_as_long_as_it_was_told.py -q`.
