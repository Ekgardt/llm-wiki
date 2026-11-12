# Tests of `scripts/install_control.py`

- `tests/test_the_guide_matches_the_passes.py`: The user guide says what the passes do and the limits they run under; no hint drops extras.

`scripts/install_control.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_guide_matches_the_passes.py -q`.
