# Tests of `scripts/markdown_transaction.py`

- `tests/test_the_repair_commands_open_the_adopted_queue.py`: `quarantine-corrupt` and `purge-corrupt` work on a vault that adopted V3.

`scripts/markdown_transaction.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_repair_commands_open_the_adopted_queue.py -q`.
