# Tests of `scripts/generation_maintenance.py`

- `tests/test_doctor_maintenance_owner_v3.py`: Doctor's maintenance fence on an adopted Reliability V3 vault (NEW-109).

`scripts/generation_maintenance.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_doctor_maintenance_owner_v3.py -q`.
