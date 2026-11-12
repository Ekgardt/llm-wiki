# Whether a vault's reliability runtime is the complete adopted v3 layout

`scripts/reliability_v3_validation.py` and the tests that exercise them most.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_backup_takes_what_any_installed_vault_holds.py tests/test_capture_inspection_writer.py tests/test_the_installed_check_reads_every_row.py -q`.
