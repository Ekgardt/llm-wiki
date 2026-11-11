# Tests of `scripts/lsp_security.py`

- `tests/test_the_scanner_scales_on_a_coarse_clock.py`: The POSIX path scanner scales near-linearly, measured on a clock that can see it.

`scripts/lsp_security.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_scanner_scales_on_a_coarse_clock.py -q`.
