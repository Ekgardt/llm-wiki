# Tests of `scripts/queue_core.py`

- `tests/test_worker_text_compatibility.py`: Moved worker values and text helper preserve original import/pickle identities.

`scripts/queue_core.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_worker_text_compatibility.py -q`.
