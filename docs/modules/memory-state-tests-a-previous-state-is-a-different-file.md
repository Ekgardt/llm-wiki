# Tests of `scripts/memory_state.py`

- `tests/test_a_previous_state_is_a_different_file.py`: `.previous` stays a separate copy of the version before the last change (audit 2026-09-27 C-20).

`scripts/memory_state.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_previous_state_is_a_different_file.py -q`.
