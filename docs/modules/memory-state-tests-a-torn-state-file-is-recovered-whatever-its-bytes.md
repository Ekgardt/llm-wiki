# Tests of `scripts/memory_state.py`

- `tests/test_a_torn_state_file_is_recovered_whatever_its_bytes.py`: A torn `run/state.json` need not be valid UTF-8, and a reader never gets a list.

`scripts/memory_state.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_torn_state_file_is_recovered_whatever_its_bytes.py -q`.
