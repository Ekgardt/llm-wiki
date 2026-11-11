# Tests of `scripts/generation_search.py`

- `tests/test_a_link_list_is_not_an_answer.py`: A chunk that is mostly links ranks behind prose (audit 2026-09-26 C-10).

`scripts/generation_search.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_link_list_is_not_an_answer.py -q`.
