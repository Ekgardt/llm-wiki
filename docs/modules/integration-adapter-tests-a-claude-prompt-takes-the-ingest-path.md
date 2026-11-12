# Tests of `scripts/integration_adapter.py`

- `tests/test_a_claude_prompt_takes_the_ingest_path.py`: A Claude prompt takes the same ingest path as every host, inside the host's time.

`scripts/integration_adapter.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_claude_prompt_takes_the_ingest_path.py -q`.
