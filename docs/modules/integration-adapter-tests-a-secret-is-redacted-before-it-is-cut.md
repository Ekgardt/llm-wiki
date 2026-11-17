# Tests of `scripts/integration_adapter.py`

- `tests/test_a_secret_is_redacted_before_it_is_cut.py`: A text is redacted whole, then cut: no secret is judged by a fragment (audit 2026-09-27 B-4).

`scripts/integration_adapter.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_secret_is_redacted_before_it_is_cut.py -q`.
