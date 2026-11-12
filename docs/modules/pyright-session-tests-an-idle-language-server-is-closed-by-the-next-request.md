# Tests of `scripts/pyright_session.py`

- `tests/test_an_idle_language_server_is_closed_by_the_next_request.py`: A server nobody has used for five minutes is closed by the next request.

`scripts/pyright_session.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_idle_language_server_is_closed_by_the_next_request.py -q`.
