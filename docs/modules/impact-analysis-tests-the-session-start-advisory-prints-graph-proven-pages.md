# Tests of `scripts/impact_analysis.py`

- `tests/test_the_session_start_advisory_prints_graph_proven_pages.py`: The session-start impact advisory names the pages the graph proved, within a budget.

`scripts/impact_analysis.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_session_start_advisory_prints_graph_proven_pages.py -q`.
