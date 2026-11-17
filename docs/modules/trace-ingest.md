# Ingest `execution-trace/v1` edges and serve them beside the static graph

`scripts/trace_ingest.py` and the tests that exercise them most.

`CODE-09`. The static extractor leaves an *observation*, never a CALLS assertion, wherever a call reaches its target through a value it cannot type. Measured on this repository's live generation, that is the largest single population in the graph. A runtime trace resolves exactly those receivers, because the program itself decided them.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_trace_ingest.py -q`.
