# Tests of `scripts/capture_diagnostics.py`

- `tests/test_failure_counters_do_not_expire_by_kind_count.py`: A new diagnostic kind cannot erase previously counted failures.

`scripts/capture_diagnostics.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_failure_counters_do_not_expire_by_kind_count.py -q`.
