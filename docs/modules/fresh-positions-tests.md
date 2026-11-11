# Tests of `scripts/fresh_positions.py`

- `tests/test_a_fresh_line_belongs_to_the_symbol_it_names.py`: Audit 3, A3/A4: a line or a block read from the file belongs to the symbol named.

`scripts/fresh_positions.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_fresh_line_belongs_to_the_symbol_it_names.py -q`.
