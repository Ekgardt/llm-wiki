# Tests of `scripts/generation_search.py`

- `tests/test_a_title_lifts_only_the_whole_question.py`: The Markdown fallback lifts a title only when it holds the whole question (audit 2026-09-26 C-10).

`scripts/generation_search.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_title_lifts_only_the_whole_question.py -q`.
