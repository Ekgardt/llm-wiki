# Durable breadcrumb publication on the existing fenced v3 capture boundary

`scripts/breadcrumb_storage.py` and the tests that exercise them most.

This module does not enable host producers. Records are accepted only once the complete manifest is durable; queue registration can then be retried without the producer's memory or another model call.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/breadcrumb_storage_helpers.py tests/test_breadcrumb_recovery.py tests/test_breadcrumb_storage.py tests/test_capture_inspection_recognizes_runtime_staging.py -q`.
