# Read one stored breadcrumb bundle back from the private capture runtime

`scripts/breadcrumb_bundle.py` and the tests that exercise them most.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_breadcrumb_health.py tests/test_breadcrumb_scan.py tests/test_capture_uses_the_host_budget.py tests/test_pending_recovery_rereads_a_completed_publication.py -q`.
