# Tests of `scripts/evidence_graph.py`

- `tests/test_graph_seed_occurrences_do_not_multiply_evidence.py`: Seed occurrences are membership, not evidence multiplicity.

`scripts/evidence_graph.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_graph_seed_occurrences_do_not_multiply_evidence.py -q`.
