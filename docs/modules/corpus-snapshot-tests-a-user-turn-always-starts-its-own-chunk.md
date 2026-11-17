# Tests of `scripts/corpus_snapshot.py`

- `tests/test_a_user_turn_always_starts_its_own_chunk.py`: A short user turn is a chunk of its own; a short assistant reply still folds.

`scripts/corpus_snapshot.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_user_turn_always_starts_its_own_chunk.py -q`.
