# Tests of `scripts/retrieval.py`

- `tests/test_a_grounded_answer_keeps_automatic_search.py`: Automatic grounded retrieval must keep the planner's semantic lanes.

`scripts/retrieval.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_grounded_answer_keeps_automatic_search.py -q`.
