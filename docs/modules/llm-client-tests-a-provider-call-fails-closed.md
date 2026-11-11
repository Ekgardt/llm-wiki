# Tests of `scripts/llm_client.py`

- `tests/test_a_provider_call_fails_closed.py`: A provider call fails closed: unknown isolation flags, no call; an unknown provider, none.

`scripts/llm_client.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_provider_call_fails_closed.py -q`.
