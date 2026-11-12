# Tests of `scripts/evidence_graph.py`

`tests/test_an_open_that_runs_out_of_time_closes_its_database.py` exercise `scripts/evidence_graph.py` through its public names.

`scripts/evidence_graph.py` itself is not part of this module: the packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_open_that_runs_out_of_time_closes_its_database.py -q`.
