# Tests of `scripts/llm_client.py`

- `tests/test_an_ollama_prompt_gets_the_room_it_needs.py`: An Ollama call carries a window that holds its prompt, or fails out loud.

`scripts/llm_client.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_ollama_prompt_gets_the_room_it_needs.py -q`.
