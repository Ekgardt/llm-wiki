# Tests of `scripts/code_graph.py`

- `tests/test_the_live_graph_reads_only_its_own_files.py`: The live graph parses only regular files of its own tree, within a bound (audit C-41).

`scripts/code_graph.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_live_graph_reads_only_its_own_files.py -q`.
