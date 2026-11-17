# Tests of `scripts/doctor.py`

- `tests/test_doctor_reads_a_v3_queue_owner.py`: Doctor reads a live owner of the adopted queue by the v3 names.

`scripts/doctor.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_doctor_reads_a_v3_queue_owner.py -q`.
