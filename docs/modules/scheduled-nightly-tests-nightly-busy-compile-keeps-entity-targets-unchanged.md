# Tests of `scripts/scheduled_nightly.py`

- `tests/test_nightly_busy_compile_keeps_entity_targets_unchanged.py`: Exhausting an idle wait does not authorize changing another compiler's inputs.

`scripts/scheduled_nightly.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_nightly_busy_compile_keeps_entity_targets_unchanged.py -q`.
