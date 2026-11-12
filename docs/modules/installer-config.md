# Shared, bounded configuration helpers for the native installers

`scripts/installer_config.py` and the tests that exercise them most.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_percent_sign_in_a_cron_line.py tests/test_both_installers_read_the_claude_file_alike.py tests/test_dependency_environments.py tests/test_installer_config.py tests/test_the_vault_runs_from_its_own_venv.py -q`.
