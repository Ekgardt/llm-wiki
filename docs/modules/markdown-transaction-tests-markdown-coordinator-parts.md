# Tests of `scripts/markdown_transaction.py`

- `tests/markdown_coordinator_parts.py`: Stand-ins reach every part of the composed Markdown coordinator that looks a name up.

`scripts/markdown_transaction.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/markdown_coordinator_parts.py -q`.
