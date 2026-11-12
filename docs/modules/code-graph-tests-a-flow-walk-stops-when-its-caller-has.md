# Tests of `scripts/code_graph.py`

`tests/test_a_flow_walk_stops_when_its_caller_has.py` exercise `scripts/code_graph.py` through its public names.

`scripts/code_graph.py` itself is not part of this module: the packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_flow_walk_stops_when_its_caller_has.py -q`.
