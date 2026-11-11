# Tests of `scripts/llm_wiki/doctor_transactions.py`

- `tests/test_the_health_line_names_what_needs_attention.py`: A health line must say which thing wants attention, not that something does.

`scripts/llm_wiki/doctor_transactions.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_health_line_names_what_needs_attention.py -q`.
