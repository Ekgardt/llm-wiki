# Tests of `scripts/llm_wiki/doctor_transactions.py`

- `tests/test_doctor_transaction_inventory_uses_its_deadline.py`: A complete transaction inventory is bounded by time, not record count.

`scripts/llm_wiki/doctor_transactions.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_doctor_transaction_inventory_uses_its_deadline.py -q`.
