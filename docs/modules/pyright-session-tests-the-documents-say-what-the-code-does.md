# Tests filed with `scripts/pyright_session.py`

- `tests/test_the_documents_say_what_the_code_does.py`: The public documents describe the code as it is, not as it was.

These files do not import `scripts/pyright_session.py`, the module they are filed with. The product code they import directly is carried in this packet as follows:

- `scripts/llm_wiki/pyright_session_startup.py`: its exact derived description.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_documents_say_what_the_code_does.py -q`.
