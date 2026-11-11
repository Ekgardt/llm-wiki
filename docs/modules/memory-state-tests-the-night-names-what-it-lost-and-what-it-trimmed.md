# Tests of `scripts/memory_state.py`

- `tests/test_the_night_names_what_it_lost_and_what_it_trimmed.py`: Incomplete nights stay visible, scheduler logs are bounded, and the daily archiver actually runs.

`scripts/memory_state.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_night_names_what_it_lost_and_what_it_trimmed.py -q`.
