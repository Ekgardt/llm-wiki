# Tests of `scripts/memory_state.py`

- `tests/test_the_open_day_waits_for_the_nightly.py`: The session start compiles closed days only; the day still being appended to waits.

`scripts/memory_state.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_open_day_waits_for_the_nightly.py -q`.
