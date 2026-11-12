# Tests of `scripts/impact_analysis.py`

- `tests/test_an_impact_answer_outlives_its_note_scan.py`: A failed note scan costs the word-match pages, never the impact answer (audit C-35).

`scripts/impact_analysis.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_impact_answer_outlives_its_note_scan.py -q`.
