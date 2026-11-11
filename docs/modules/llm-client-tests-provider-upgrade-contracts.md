# Tests of `scripts/llm_client.py`

- `tests/test_provider_upgrade_contracts.py`: Upgrades must refresh observed capabilities without a release allowlist.

`scripts/llm_client.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_provider_upgrade_contracts.py -q`.
