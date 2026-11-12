# Tests of `scripts/pyright_session.py`

- `tests/test_the_documents_say_what_the_code_does.py`: The public documents describe the code as it is, not as it was.

`scripts/pyright_session.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_documents_say_what_the_code_does.py -q`.
