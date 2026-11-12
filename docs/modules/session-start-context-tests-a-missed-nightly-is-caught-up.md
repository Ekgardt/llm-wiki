# Tests of `scripts/session_start_context.py`

`tests/test_a_missed_nightly_is_caught_up.py` exercise `scripts/session_start_context.py` through its public names.

`scripts/session_start_context.py` itself is not part of this module: the packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_missed_nightly_is_caught_up.py -q`.
