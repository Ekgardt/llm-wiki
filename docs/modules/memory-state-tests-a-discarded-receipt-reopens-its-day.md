# Tests of `scripts/memory_state.py`

- `tests/test_a_discarded_receipt_reopens_its_day.py`: Discarding a receipt makes its day pending again, and `--all` says it is idle.

`scripts/memory_state.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_discarded_receipt_reopens_its_day.py -q`.
