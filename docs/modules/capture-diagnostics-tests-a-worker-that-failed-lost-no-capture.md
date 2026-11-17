# Tests of `scripts/capture_diagnostics.py`

- `tests/test_a_worker_that_failed_lost_no_capture.py`: A capture worker's failure is retried work, and its record names the real cause.

`scripts/capture_diagnostics.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_worker_that_failed_lost_no_capture.py -q`.
