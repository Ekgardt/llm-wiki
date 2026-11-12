# Tests of `scripts/query_memory.py`

- `tests/test_the_calendar_does_the_arithmetic.py`: A gap between dates is computed by the calendar, not narrated by the model.

`scripts/query_memory.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_calendar_does_the_arithmetic.py -q`.
