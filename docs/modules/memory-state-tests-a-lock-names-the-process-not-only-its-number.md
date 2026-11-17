# Tests of `scripts/memory_state.py`

- `tests/test_a_lock_names_the_process_not_only_its_number.py`: A lock file records the identity of the process that took it, not only its PID.

`scripts/memory_state.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_lock_names_the_process_not_only_its_number.py -q`.
