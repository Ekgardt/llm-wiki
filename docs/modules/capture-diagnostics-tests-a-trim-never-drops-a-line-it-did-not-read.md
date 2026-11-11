# Tests of `scripts/capture_diagnostics.py`

- `tests/test_a_trim_never_drops_a_line_it_did_not_read.py`: A trim of the failure trail never crosses an append, so no reason is erased.

`scripts/capture_diagnostics.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_trim_never_drops_a_line_it_did_not_read.py -q`.
