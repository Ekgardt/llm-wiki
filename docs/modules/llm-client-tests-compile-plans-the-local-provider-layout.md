# Tests of `scripts/llm_client.py`

- `tests/test_compile_plans_the_local_provider_layout.py`: Packing and final fit include the local Codex input's actual framing.

`scripts/llm_client.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_compile_plans_the_local_provider_layout.py -q`.
