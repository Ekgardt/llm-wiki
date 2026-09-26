# The docs name the rebuild command

Date: 2026-09-25. Audit item B-26 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code)

- `doctor --repair` runs `_repair_generations_action`, which repairs the generation catalog and
  returns unless `rebuild_generation` is set; only `--rebuild-generation` (or the nightly
  refresh, or `search_memory.py --rebuild`) builds a generation.
- `docs/USER-GUIDE.md` said vectors are built by "a generation refresh — the nightly
  maintenance pass, or `uv run python scripts/doctor.py --repair`", and "Search returns
  nothing" advised `--repair` to "rebuild the generation".

## Source

- Diátaxis, "Reference", https://diataxis.fr/reference/ (fetched 2026-09-25): "There should be
  no doubt or ambiguity in reference; it should be wholly authoritative."

## Decision

- Both passages name `--rebuild-generation`. A test reads every README and top-level doc and
  fails on a paragraph that names `doctor.py --repair` together with rebuilding the generation
  without naming `--rebuild-generation`; it catches the two old passages.

## Files

- `docs/USER-GUIDE.md`
- `tests/test_the_docs_name_the_command_that_rebuilds_the_generation.py`
- `CHANGELOG.md`
