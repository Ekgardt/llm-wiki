# Tests of `scripts/llm_client.py`

- `tests/test_provider_timeout_is_not_an_empty_answer.py`: A provider that ran out of time did not answer with nothing.

`scripts/llm_client.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_provider_timeout_is_not_an_empty_answer.py -q`.
