# Build a clean export archive of the vault via `git archive`

`scripts/export_vault.py` and the tests that exercise them most.

Makes the right thing the easy thing: one command produces a zip/tar containing ONLY git-tracked files at the current HEAD. No `.venv/`, no `.git/`, no `.pytest_cache/`, no `.obsidian/workspace.json`, no `.claude/settings.local.json`, no `gitleaks-report.json` — all of which are gitignored and cannot leak through `git archive` by construction.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_audit_runtime_contracts.py tests/test_export_strict.py -q`.
