# Tests of `scripts/integration_adapter.py`

- `tests/test_a_transient_transcript_never_outlives_its_event.py`: A transcript copy made for one event is removed even when its intent is not published.

`scripts/integration_adapter.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_transient_transcript_never_outlives_its_event.py -q`.
