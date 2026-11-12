# Tests of `scripts/flush_memory.py`

- `tests/test_a_redrive_reuses_the_decision_its_parent_indexed.py`: A redrive reuses the semantic decision its dead parent already indexed.

`scripts/flush_memory.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_redrive_reuses_the_decision_its_parent_indexed.py -q`.
