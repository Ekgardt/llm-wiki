# Tests of `scripts/llm_client.py`

- `tests/test_every_provider_names_a_death_and_a_deadline.py`: A codex that died, and any provider that ran out of time, are named as such.

`scripts/llm_client.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_every_provider_names_a_death_and_a_deadline.py -q`.
