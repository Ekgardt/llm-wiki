# Tests of `scripts/capture_adoption.py`

`tests/test_both_recovery_passes_look_past_a_bad_head.py` exercise `scripts/capture_adoption.py` through its public names.

`scripts/capture_adoption.py` itself is not part of this module: the packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_both_recovery_passes_look_past_a_bad_head.py -q`.
