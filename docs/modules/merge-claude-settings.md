# Merge LLM-wiki Claude Code hooks into user settings.json safely

`scripts/merge_claude_settings.py`, `scripts/integration_config_backup.py` and the tests that exercise them most.

Does NOT wipe the user's existing hooks/permissions. Strategy: - Backup existing settings to settings.json.bak-llm-wiki-<timestamp> - For each hook event we own: drop prior entries whose command mentions our scripts, then append the template entries - Union permissions.allow / permissions.deny - Ensure env.LLM_WIKI_ROOT / LLM_WIKI_STATE_ROOT are set - Write merged JSON with trailing newline

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_merge_claude_settings.py -q`.
