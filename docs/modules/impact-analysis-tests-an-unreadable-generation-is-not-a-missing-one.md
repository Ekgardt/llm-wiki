# Tests of `scripts/impact_analysis.py`

- `tests/test_an_unreadable_generation_is_not_a_missing_one.py`: A damaged generation is named as damaged, and a wrong argument is not hidden.

`scripts/impact_analysis.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_unreadable_generation_is_not_a_missing_one.py -q`.
