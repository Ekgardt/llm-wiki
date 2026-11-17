# Tests of `scripts/capture_intent_records.py`

- `tests/test_breadcrumb_terminal_proof.py`: A claimed success cannot replace durable destination evidence.

`scripts/capture_intent_records.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_breadcrumb_terminal_proof.py -q`.
