# Migrate existing markdown pages to OKF (Open Knowledge Format v0.1)

`scripts/migrate_to_okf.py` and the tests that exercise them most.

OKF requires: 1. Every non-reserved .md file has YAML frontmatter. 2. Frontmatter contains a non-empty `type:` field.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_migrate_to_okf_exemptions.py tests/test_okf_migration_binds_content_to_its_source_snapshot.py -q`.
