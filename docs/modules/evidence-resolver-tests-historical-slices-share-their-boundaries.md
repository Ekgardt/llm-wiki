# Tests of `scripts/evidence_resolver.py`

- `tests/test_historical_slices_share_their_boundaries.py`: Historical citation lookup keeps full hash proofs without repeated source scans.

`scripts/evidence_resolver.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_historical_slices_share_their_boundaries.py -q`.
