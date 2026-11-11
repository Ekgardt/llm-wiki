# Tests of `scripts/evidence_resolver.py`

- `tests/test_doctor_reads_the_supported_daily_size.py`: Doctor must inspect genuine receipts for the supported daily source family.

`scripts/evidence_resolver.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_doctor_reads_the_supported_daily_size.py -q`.
