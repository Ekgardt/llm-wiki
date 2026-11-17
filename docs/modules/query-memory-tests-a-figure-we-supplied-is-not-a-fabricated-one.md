# Tests of `scripts/query_memory.py`

- `tests/test_a_figure_we_supplied_is_not_a_fabricated_one.py`: A number we handed the model for a span is not a number it made up.

`scripts/query_memory.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_figure_we_supplied_is_not_a_fabricated_one.py -q`.
