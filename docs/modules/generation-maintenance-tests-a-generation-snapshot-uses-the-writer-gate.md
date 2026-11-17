# Tests of `scripts/generation_maintenance.py`

- `tests/test_a_generation_snapshot_uses_the_writer_gate.py`: A generation snapshot participates in the canonical Markdown writer gate.

`scripts/generation_maintenance.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_generation_snapshot_uses_the_writer_gate.py -q`.
