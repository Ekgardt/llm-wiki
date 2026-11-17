# Tests filed with `scripts/project_checkpoint.py`

- `tests/adopted_capture_vault.py`: A temporary vault whose capture queue is adopted, with the adapter pointed at it.

These files do not import `scripts/project_checkpoint.py`, the module they are filed with. The product code they import directly is carried in this packet as follows:

- `scripts/installed_memory_repair.py`: its exact derived description.
- `scripts/integration_adapter.py`: its exact derived description.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/adopted_capture_vault.py -q`.
