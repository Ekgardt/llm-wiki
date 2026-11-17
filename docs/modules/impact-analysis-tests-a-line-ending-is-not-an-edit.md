# Tests of `scripts/impact_analysis.py`

- `tests/test_a_line_ending_is_not_an_edit.py`: On a `core.autocrlf` checkout an edit names its own symbol, exactly (audit 2026-09-26 B-8).

`scripts/impact_analysis.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_line_ending_is_not_an_edit.py -q`.
