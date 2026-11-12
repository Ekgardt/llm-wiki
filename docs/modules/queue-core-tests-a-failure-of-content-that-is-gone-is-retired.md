# Tests of `scripts/queue_core.py`

- `tests/test_a_failure_of_content_that_is_gone_is_retired.py`: A compile failure recorded for bytes the day no longer has is retired.

`scripts/queue_core.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_failure_of_content_that_is_gone_is_retired.py -q`.
