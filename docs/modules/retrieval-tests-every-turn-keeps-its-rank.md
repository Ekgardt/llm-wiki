# Tests of `scripts/retrieval.py`

- `tests/test_every_turn_keeps_its_rank.py`: A turn of a conversation keeps its rank; only a compiled page's later chunks wait.

`scripts/retrieval.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_every_turn_keeps_its_rank.py -q`.
