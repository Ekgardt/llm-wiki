# Tests of `scripts/queue_core.py`

- `tests/test_an_expired_v2_fence_does_not_refuse_adoption.py`: A leftover v2 fence does not lock a vault out of adoption, and a refusal says why.

`scripts/queue_core.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_expired_v2_fence_does_not_refuse_adoption.py -q`.
