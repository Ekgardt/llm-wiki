# Tests of `scripts/compile_receipts.py`

- `tests/test_one_part_of_a_day_per_batch.py`: Unverified parts of one day cannot share a compile batch.

`scripts/compile_receipts.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_one_part_of_a_day_per_batch.py -q`.
