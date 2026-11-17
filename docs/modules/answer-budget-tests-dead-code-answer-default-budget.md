# Tests of `scripts/answer_budget.py`

- `tests/test_dead_code_answer_default_budget.py`: `find_dead_code` costs what it claims, and a cut lands where doubt is.

`scripts/answer_budget.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_dead_code_answer_default_budget.py -q`.
