# Tests of `scripts/claims.py`

- `tests/test_the_contradiction_check_compares_what_can_matter.py`: The contradiction check compares only what can change its answer.

`scripts/claims.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_contradiction_check_compares_what_can_matter.py -q`.
