# Structural ownership adapters for supported IDE hook configurations

`scripts/integration_hook_config.py` and the tests that exercise them most.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/codex_installed_provider_bundle_reaches_launch_helpers.py tests/integration_injection_helpers.py tests/test_claude_provider_projection_round_trips.py tests/test_codex_installed_provider_bundle_reaches_launch.py tests/test_codex_provider_bootstrap_preserves_runtime_choice.py tests/test_the_installed_choice_of_provider_travels_whole.py -q`.
