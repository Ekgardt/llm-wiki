# Tests of `scripts/generation_search.py`

- `tests/test_a_row_that_carries_its_text_carries_no_cut_of_it.py`: A recall row that carries its text carries no cut of it (audit 2026-09-27 C-16).

`scripts/generation_search.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_row_that_carries_its_text_carries_no_cut_of_it.py -q`.
