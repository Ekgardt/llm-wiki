# Tests of `scripts/impact_analysis.py`

- `tests/test_an_impact_diff_can_be_stopped.py`: The per-edit diff runs where a deadline and a cancel reach it (audit 2026-09-27 A-5).

`scripts/impact_analysis.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_impact_diff_can_be_stopped.py -q`.
