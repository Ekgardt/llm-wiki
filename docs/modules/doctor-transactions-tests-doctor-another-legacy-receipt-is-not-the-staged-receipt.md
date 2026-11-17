# Tests of `scripts/llm_wiki/doctor_transactions.py`

- `tests/test_doctor_another_legacy_receipt_is_not_the_staged_receipt.py`: A retained legacy attempt cannot borrow another receipt's authority.

`scripts/llm_wiki/doctor_transactions.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_doctor_another_legacy_receipt_is_not_the_staged_receipt.py -q`.
