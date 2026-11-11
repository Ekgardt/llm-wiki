# Tests of `scripts/retrieval.py`

- `tests/test_semantic_leg_reaches_the_answer.py`: Regression tests for the four ways the semantic leg failed to reach an answer.

`scripts/retrieval.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_semantic_leg_reaches_the_answer.py -q`.
