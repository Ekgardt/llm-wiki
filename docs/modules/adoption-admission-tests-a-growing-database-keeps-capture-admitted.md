# Tests of `scripts/llm_wiki/adoption_admission.py`

- `tests/test_a_growing_database_keeps_capture_admitted.py`: SQLite admission validates identity/schema without a whole-file read budget.

`scripts/llm_wiki/adoption_admission.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_growing_database_keeps_capture_admitted.py -q`.
