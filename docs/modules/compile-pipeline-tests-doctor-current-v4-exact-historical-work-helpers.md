# Tests of `scripts/compile_pipeline.py`

`tests/doctor_current_v4_exact_historical_work_helpers.py` exercise `scripts/compile_pipeline.py` through its public names.

`scripts/compile_pipeline.py` itself is not part of this module: the packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/doctor_current_v4_exact_historical_work_helpers.py -q`.
