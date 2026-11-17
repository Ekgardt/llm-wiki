# Tests of `scripts/queue_core.py`

- `tests/test_work_does_not_count_a_capture_it_cannot_claim.py`: `work` does not count a capture task it cannot claim.

`scripts/queue_core.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_work_does_not_count_a_capture_it_cannot_claim.py -q`.
