# The guard that stops a test from writing into the owner's live vault

- `tests/test_vault_leak_guard.py`: The guard that stops a test from writing into the owner's live vault.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_vault_leak_guard.py -q`.
