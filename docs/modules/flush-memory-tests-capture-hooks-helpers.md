# Tests of `scripts/flush_memory.py`

`tests/capture_hooks_helpers.py` exercise `scripts/flush_memory.py` through its public names.

`scripts/flush_memory.py` itself is not part of this module: the packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/capture_hooks_helpers.py -q`.
