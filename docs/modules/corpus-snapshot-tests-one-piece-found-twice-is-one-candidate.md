# Tests of `scripts/corpus_snapshot.py`

- `tests/test_one_piece_found_twice_is_one_candidate.py`: A piece two searches found leads the pieces one search found, whatever shape its row has.

`scripts/corpus_snapshot.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_one_piece_found_twice_is_one_candidate.py -q`.
