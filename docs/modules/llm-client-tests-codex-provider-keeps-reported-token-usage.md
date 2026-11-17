# Tests of `scripts/llm_client.py`

- `tests/test_codex_provider_keeps_reported_token_usage.py`: The Codex final text and its completed-turn counters survive the common boundary.

`scripts/llm_client.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_codex_provider_keeps_reported_token_usage.py -q`.
