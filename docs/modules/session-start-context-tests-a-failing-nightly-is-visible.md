# Tests of `scripts/session_start_context.py`

- `tests/test_a_failing_nightly_is_visible.py`: The nightly failed for five days and every session start said nothing.

`scripts/session_start_context.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_failing_nightly_is_visible.py -q`.
