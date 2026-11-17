# Tests of `scripts/llm_wiki/doctor_transactions.py`

- `tests/test_doctor_retains_transaction_scan_failure_class.py`: Incomplete transaction inspection retains its cause and deletion refusal.

`scripts/llm_wiki/doctor_transactions.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_doctor_retains_transaction_scan_failure_class.py -q`.
