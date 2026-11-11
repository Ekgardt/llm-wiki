# Tests of `scripts/event_envelope.py`

- `tests/test_native_event_json_is_decoded_once.py`: Source-authority and facade consumers retain the native reader contract.

`scripts/event_envelope.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_native_event_json_is_decoded_once.py -q`.
