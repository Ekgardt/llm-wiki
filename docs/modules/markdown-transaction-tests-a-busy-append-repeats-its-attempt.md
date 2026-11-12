# Tests of `scripts/markdown_transaction.py`

- `tests/test_a_busy_append_repeats_its_attempt.py`: A database busy past its timeout repeats the append attempt instead of failing it (audit 2026-09-26 B-27).

`scripts/markdown_transaction.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_busy_append_repeats_its_attempt.py -q`.
