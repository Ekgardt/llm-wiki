# Tests of `scripts/integration_adapter.py`

- `tests/test_the_six_capture_corrections_the_first_round_left.py`: The six items of C-F15 the first round left: reporting, naming, dating and re-entry.

`scripts/integration_adapter.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_six_capture_corrections_the_first_round_left.py -q`.
