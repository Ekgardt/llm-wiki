# Tests of `scripts/scheduled_nightly.py`

- `tests/test_a_nightly_wait_does_not_predict_another_owners_exit.py`: A bounded wait cannot establish the lifecycle of an existing compiler.

`scripts/scheduled_nightly.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_nightly_wait_does_not_predict_another_owners_exit.py -q`.
