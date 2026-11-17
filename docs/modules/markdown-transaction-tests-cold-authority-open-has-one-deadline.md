# Tests of `scripts/markdown_transaction.py`

- `tests/test_cold_authority_open_has_one_deadline.py`: A caller budget includes cold database setup and adoption admission.

`scripts/markdown_transaction.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_cold_authority_open_has_one_deadline.py -q`.
