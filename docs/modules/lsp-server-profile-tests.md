# Tests of `scripts/lsp_server_profile.py`

- `tests/test_native_language_server.py`: A managed server that is a native executable, built rather than unpacked.
- `tests/test_the_second_installer_gets_the_first_ones_guarantees.py`: Audit 3, B21: the language-server installer gets the Pyright one's posture.

`scripts/lsp_server_profile.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_native_language_server.py tests/test_the_second_installer_gets_the_first_ones_guarantees.py -q`.
