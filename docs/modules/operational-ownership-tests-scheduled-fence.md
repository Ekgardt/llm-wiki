# Tests of `scripts/operational_ownership.py`

- `tests/test_scheduled_fence.py`: The nightly and weekly passes take the canonical fence (decision 2026-09-10).

`scripts/operational_ownership.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_scheduled_fence.py -q`.
