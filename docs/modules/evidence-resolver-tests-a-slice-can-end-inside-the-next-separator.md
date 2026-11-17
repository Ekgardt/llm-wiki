# Tests of `scripts/evidence_resolver.py`

- `tests/test_a_slice_can_end_inside_the_next_separator.py`: A day that grew keeps the citations written before it grew.

`scripts/evidence_resolver.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_slice_can_end_inside_the_next_separator.py -q`.
