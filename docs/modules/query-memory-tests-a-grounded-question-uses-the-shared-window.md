# Tests of `scripts/query_memory.py`

- `tests/test_a_grounded_question_uses_the_shared_window.py`: Question admission keeps the whole request and checks the actual prompt window.

`scripts/query_memory.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_grounded_question_uses_the_shared_window.py -q`.
