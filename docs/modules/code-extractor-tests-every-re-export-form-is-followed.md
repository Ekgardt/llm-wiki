# Tests of `scripts/code_extractor.py`

- `tests/test_every_re_export_form_is_followed.py`: A re-export made by `import *`, inside a `try`, or by a later import is followed (audit 2026-09-27 C-7).

`scripts/code_extractor.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_every_re_export_form_is_followed.py -q`.
