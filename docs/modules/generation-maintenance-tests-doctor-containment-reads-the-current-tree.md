# Tests of `scripts/generation_maintenance.py`

- `tests/test_doctor_containment_reads_the_current_tree.py`: Containment still follows current physical authority after allocation changes.

`scripts/generation_maintenance.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_doctor_containment_reads_the_current_tree.py -q`.
