# Tests of `scripts/markdown_transaction.py`

- `tests/test_a_spent_checkpoint_reservation_is_dropped.py`: A reservation on a checkpoint that another attempt committed is dropped at once.

`scripts/markdown_transaction.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_spent_checkpoint_reservation_is_dropped.py -q`.
