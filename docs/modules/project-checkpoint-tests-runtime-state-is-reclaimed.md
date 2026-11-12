# Tests of `scripts/project_checkpoint.py`

- `tests/test_runtime_state_is_reclaimed.py`: Recovery that a session hook cannot perform must still happen, unattended.

`scripts/project_checkpoint.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_runtime_state_is_reclaimed.py -q`.
