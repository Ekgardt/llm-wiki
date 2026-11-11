# Tests of `scripts/capture_diagnostics.py`

- `tests/test_hook_error_redaction.py`: Every hook error sink protects credentials and its one-record boundary.

`scripts/capture_diagnostics.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_hook_error_redaction.py -q`.
