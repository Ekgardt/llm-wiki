# Tests of `scripts/operational_ownership.py`

- `tests/test_ownership_actor_is_the_agent.py`: The actor of a lease is the agent taking it, not the machine account.

`scripts/operational_ownership.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_ownership_actor_is_the_agent.py -q`.
