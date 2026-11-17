# Tests of `scripts/memory_state.py`

- `tests/test_a_payload_is_written_as_the_bytes_it_is.py`: A descriptor this repository writes bytes to is untranslated on every platform.

`scripts/memory_state.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_payload_is_written_as_the_bytes_it_is.py -q`.
