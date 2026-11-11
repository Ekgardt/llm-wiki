# Tests of `scripts/queue_core.py`

`tests/test_compile_parent_lock_handoff.py` exercise `scripts/queue_core.py` through its public names.

`scripts/queue_core.py` itself is not part of this module: the packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_compile_parent_lock_handoff.py -q`.
