# Tests of `scripts/reflection.py`

- `tests/test_reflection_keeps_only_the_live_claim_ledger.py`

`scripts/reflection.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_reflection_keeps_only_the_live_claim_ledger.py -q`.
