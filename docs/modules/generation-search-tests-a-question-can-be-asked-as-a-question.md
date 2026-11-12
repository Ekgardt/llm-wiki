# Tests of `scripts/generation_search.py`

- `tests/test_a_question_can_be_asked_as_a_question.py`: FTS5 puts an implicit AND between bare terms, and it cost us whole answers.

`scripts/generation_search.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_question_can_be_asked_as_a_question.py -q`.
