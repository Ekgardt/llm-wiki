# Tests of `scripts/capture_diagnostics.py`

- `tests/test_a_long_session_in_the_vault_is_captured.py`: A long session in the vault is captured, and each capture keeps its own window.

`scripts/capture_diagnostics.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_long_session_in_the_vault_is_captured.py -q`.
