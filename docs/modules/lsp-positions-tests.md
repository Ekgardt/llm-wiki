# Tests of `scripts/lsp_positions.py`

- `tests/test_a_position_names_its_units.py`: A position past the line end is the line end; the schema names its units (audit 2026-09-26 C-9).

`scripts/lsp_positions.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_position_names_its_units.py -q`.
