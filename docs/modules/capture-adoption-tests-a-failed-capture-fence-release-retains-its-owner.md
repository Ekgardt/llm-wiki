# Tests of `scripts/capture_adoption.py`

- `tests/test_a_failed_capture_fence_release_retains_its_owner.py`: A blocked dependent release must not delete its canonical owner.

`scripts/capture_adoption.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_failed_capture_fence_release_retains_its_owner.py -q`.
