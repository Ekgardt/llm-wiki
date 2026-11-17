# Tests of `scripts/generation_search.py`

- `tests/test_forgotten_not_gone.py`: An archived page was unreachable even when a question named it exactly.

`scripts/generation_search.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_forgotten_not_gone.py -q`.
