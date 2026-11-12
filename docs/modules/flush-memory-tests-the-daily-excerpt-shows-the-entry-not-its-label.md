# Tests of `scripts/flush_memory.py`

- `tests/test_the_daily_excerpt_shows_the_entry_not_its_label.py`: Session start shows what a daily entry says, not the labels the worker wrote above it.

`scripts/flush_memory.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_daily_excerpt_shows_the_entry_not_its_label.py -q`.
