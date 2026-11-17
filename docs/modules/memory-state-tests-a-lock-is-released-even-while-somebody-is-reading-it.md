# Tests of `scripts/memory_state.py`

- `tests/test_a_lock_is_released_even_while_somebody_is_reading_it.py`: The state lock is removed even while another reader holds the file open.

`scripts/memory_state.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_lock_is_released_even_while_somebody_is_reading_it.py -q`.
