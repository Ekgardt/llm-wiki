# Tests of `scripts/llm_wiki/queue_schema.py`

- `tests/test_schema_literals_are_not_normalized_away.py`: Managed-schema comparison must reject changes to SQL values and identities.

`scripts/llm_wiki/queue_schema.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_schema_literals_are_not_normalized_away.py -q`.
