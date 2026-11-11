# Complete permanent sources through the existing Markdown transaction boundary

`scripts/breadcrumb_evidence.py` and the tests that exercise them most.

Publishing a source alone never grants terminal completion or runtime cleanup.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_breadcrumb_evidence.py tests/test_breadcrumb_purge.py tests/test_breadcrumb_source_publication.py -q`.
