# Tests of `scripts/model_dlp.py`

- `tests/test_a_redacted_value_keeps_its_closing_markup.py`: Text already redacted stays byte for byte; a marker never hides more credential text.
- `tests/test_dlp_finding_allowlist.py`: The DLP unlock names the finding, not the file it lives in.

`scripts/model_dlp.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_redacted_value_keeps_its_closing_markup.py tests/test_dlp_finding_allowlist.py -q`.
