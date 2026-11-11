# Tests of `scripts/lsp_security.py`

- `tests/test_a_refused_path_names_its_component_and_its_rule.py`: A refused path says which component broke which rule, and a failed build leaves nothing.

`scripts/lsp_security.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_refused_path_names_its_component_and_its_rule.py -q`.
