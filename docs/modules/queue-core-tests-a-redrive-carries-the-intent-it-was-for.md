# Tests of `scripts/queue_core.py`

- `tests/test_a_redrive_carries_the_intent_it_was_for.py`: A redriven capture task must keep its intent, and get exactly one more round.

`scripts/queue_core.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_redrive_carries_the_intent_it_was_for.py -q`.
