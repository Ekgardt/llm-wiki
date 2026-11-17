# Tests of `scripts/markdown_transaction.py`

- `tests/test_a_busy_release_is_retried_until_it_lands.py`: A canonical release refused by a busy database is retried until it lands (audit 2026-09-26 A-13).

`scripts/markdown_transaction.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_busy_release_is_retried_until_it_lands.py -q`.
