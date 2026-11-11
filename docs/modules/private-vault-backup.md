# Coherent staging images for encrypted private-vault backups

`scripts/private_vault_backup.py` and the tests that exercise them most.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/private_vault_backup_helpers.py tests/test_a_backup_image_carries_what_git_does_not.py tests/test_a_backup_snapshots_what_hooks_write.py tests/test_a_publication_counts_what_it_leaves_out.py tests/test_a_publication_is_all_or_nothing.py tests/test_a_real_restic_summary_is_accepted.py tests/test_a_refused_publication_names_its_file.py tests/test_private_vault_backup.py -q`.
