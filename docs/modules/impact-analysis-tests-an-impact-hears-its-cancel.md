# Tests of `scripts/impact_analysis.py`

- `tests/test_an_impact_hears_its_cancel.py`: An impact run stops when its caller cancels, in Git and in the mapping (audit 2026-09-26 C-9).

`scripts/impact_analysis.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_impact_hears_its_cancel.py -q`.
