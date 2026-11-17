# Tests of `scripts/retrieval.py`

- `tests/test_recall_asks_the_graph_about_relations.py`: A question about relations asks the evidence graph, and keeps dense search.

`scripts/retrieval.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_recall_asks_the_graph_about_relations.py -q`.
