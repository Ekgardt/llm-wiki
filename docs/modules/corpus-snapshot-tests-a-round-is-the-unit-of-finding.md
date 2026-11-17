# Tests of `scripts/corpus_snapshot.py`

- `tests/test_a_round_is_the_unit_of_finding.py`: A conversation is found by the round and read with its neighbours.

`scripts/corpus_snapshot.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_round_is_the_unit_of_finding.py -q`.
