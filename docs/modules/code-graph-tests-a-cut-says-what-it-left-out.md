# Tests of `scripts/code_graph.py`

- `tests/test_a_cut_says_what_it_left_out.py`: A bound that drops items either keeps the data or says what it left out (law 9).

`scripts/code_graph.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_cut_says_what_it_left_out.py -q`.
