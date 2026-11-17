# Tests of `scripts/corpus_snapshot.py`

- `tests/test_graph_record_budget_belongs_to_the_caller.py`: A complete captured universe does not inherit a hidden graph record count.

`scripts/corpus_snapshot.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_graph_record_budget_belongs_to_the_caller.py -q`.
