# Tests of `scripts/answer_budget.py`

- `tests/test_a_context_answer_fits_the_budget_it_was_given.py`: A context answer fits the budget it was given, names each item once, and counts one way (audit 2026-09-27 B-12).

`scripts/answer_budget.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_context_answer_fits_the_budget_it_was_given.py -q`.
