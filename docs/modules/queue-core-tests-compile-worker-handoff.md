# Tests of `scripts/queue_core.py`

- `tests/test_compile_worker_handoff.py`: The existing spawned worker can retain canonical compile lease authority.

`scripts/queue_core.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_compile_worker_handoff.py -q`.
