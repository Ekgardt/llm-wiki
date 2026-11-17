# Codex-friendly wrapper around the LLM-wiki multi-project memory hooks

`scripts/codex_memory.py` and the tests that exercise them most.

This script reuses the existing Claude-oriented hook implementations so Codex can pull the same per-project state and write the same daily-log breadcrumbs without forking slug/state logic.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_earlier_codex_entry_is_rewritten.py tests/test_codex_config_status_has_a_platform_independent_newline.py tests/test_codex_hooks_ownership.py tests/test_daily_result_reporting.py -q`.
