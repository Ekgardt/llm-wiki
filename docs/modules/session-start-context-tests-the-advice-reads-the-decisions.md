# Tests of `scripts/session_start_context.py`

- `tests/test_the_advice_reads_the_decisions.py`: The session-start advice about the classifier reads the decisions it made.

`scripts/session_start_context.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_advice_reads_the_decisions.py -q`.
