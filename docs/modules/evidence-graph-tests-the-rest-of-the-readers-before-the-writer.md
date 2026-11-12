# Tests of `scripts/evidence_graph.py`

- `tests/test_the_rest_of_the_readers_before_the_writer.py`: One odd record in a note or a source file no longer fails the whole generation.

`scripts/evidence_graph.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_rest_of_the_readers_before_the_writer.py -q`.
