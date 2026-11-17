# Tests of `scripts/llm_wiki/doctor_transactions.py`

- `tests/test_doctor_exact_legacy_source_outcome.py`: A historical source outcome needs complete committed receipt authority.

`scripts/llm_wiki/doctor_transactions.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_doctor_exact_legacy_source_outcome.py -q`.
