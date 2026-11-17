# Tests of `scripts/generation_search.py`

- `tests/test_a_question_finds_pages_without_a_generation.py`: With no active generation a natural question still finds the pages that answer it.

`scripts/generation_search.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_question_finds_pages_without_a_generation.py -q`.
