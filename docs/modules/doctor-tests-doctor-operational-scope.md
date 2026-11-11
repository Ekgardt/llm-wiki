# Tests of `scripts/doctor.py`

`tests/test_doctor_operational_scope.py` exercise `scripts/doctor.py` through its public names.

`scripts/doctor.py` itself is not part of this module: the packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_doctor_operational_scope.py -q`.
