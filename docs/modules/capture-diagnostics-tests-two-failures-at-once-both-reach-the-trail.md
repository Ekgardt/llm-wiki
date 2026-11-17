# Tests of `scripts/capture_diagnostics.py`

- `tests/test_two_failures_at_once_both_reach_the_trail.py`: Failures recorded at the same moment all reach the trail, and the trail stays bounded.

`scripts/capture_diagnostics.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_two_failures_at_once_both_reach_the_trail.py -q`.
