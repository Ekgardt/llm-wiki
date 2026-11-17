# Tests of `scripts/evidence_graph.py`

- `tests/test_a_publication_that_is_out_of_time_does_not_link.py`: The validation a publication runs is inside the caller's bound (G-M7, third point).

`scripts/evidence_graph.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_publication_that_is_out_of_time_does_not_link.py -q`.
