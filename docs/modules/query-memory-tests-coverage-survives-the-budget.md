# Tests of `scripts/query_memory.py`

- `tests/test_coverage_survives_the_budget.py`: Under a budget, a repeat of a page goes before the only span from another.

`scripts/query_memory.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_coverage_survives_the_budget.py -q`.
