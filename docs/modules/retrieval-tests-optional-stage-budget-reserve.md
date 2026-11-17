# Tests of `scripts/retrieval.py`

- `tests/test_optional_stage_budget_reserve.py`: An optional stage may not spend the budget the mandatory tail needs.

`scripts/retrieval.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_optional_stage_budget_reserve.py -q`.
