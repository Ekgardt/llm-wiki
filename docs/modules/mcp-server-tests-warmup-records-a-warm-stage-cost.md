# Tests of `scripts/mcp_server.py`

- `tests/test_warmup_records_a_warm_stage_cost.py`: Warming the encoder was necessary and was not sufficient.

`scripts/mcp_server.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_warmup_records_a_warm_stage_cost.py -q`.
