# Tests of `scripts/integration_adapter.py`

- `tests/test_the_evidence_fits_its_record.py`: A transcript that grows inside JSON is still captured, and a cut record says it was cut.

`scripts/integration_adapter.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_evidence_fits_its_record.py -q`.
