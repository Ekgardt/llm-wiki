# Tests of `scripts/context_budget.py`

- `tests/test_a_context_answer_sends_its_text_once.py`: A context answer sends its text once and keeps each class in the caller's order (audit 2026-09-26 B-18).
- `tests/test_selected_context_is_not_duplicated_to_fit_a_page.py`: Real source fragments must fit without implicit repeated whole-page expansion.

`scripts/context_budget.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_context_answer_sends_its_text_once.py tests/test_selected_context_is_not_duplicated_to_fit_a_page.py -q`.
