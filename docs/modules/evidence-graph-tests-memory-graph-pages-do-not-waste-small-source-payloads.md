# Tests of `scripts/evidence_graph.py`

- `tests/test_memory_graph_pages_do_not_waste_small_source_payloads.py`: Small captured sources must not each pay for a mostly empty overflow page.

`scripts/evidence_graph.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_memory_graph_pages_do_not_waste_small_source_payloads.py -q`.
