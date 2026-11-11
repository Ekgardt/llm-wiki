# Tests of `scripts/claims.py`

- `tests/test_a_blank_line_before_the_next_section.py`: A Claims ledger stays readable when a blank line and a section follow it.

`scripts/claims.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_blank_line_before_the_next_section.py -q`.
