# Tests of `scripts/capture_adoption.py`

- `tests/test_capture_intent_adoption.py`: A capture intent published but never dispatched must still reach a worker.

`scripts/capture_adoption.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_capture_intent_adoption.py -q`.
