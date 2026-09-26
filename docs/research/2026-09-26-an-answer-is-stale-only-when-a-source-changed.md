# An answer is stale only when a source changed

Date: 2026-09-26. Audit 2026-09-26 B-15 (B-19 of 2026-09-25 incomplete).

## Facts

- `mcp_server._index_is_behind` compared the newest modification time of notes,
  note directories and project files with the generation manifest's mtime. A
  touched file, an edited README the corpus never reads, or a renamed directory
  moved a time and not the corpus; the nightly then reused the parent generation
  (nothing to rebuild), so the manifest time never moved and every answer said
  "stale" for good. A removed project state was not noticed at all.
- git-update-index documentation (https://git-scm.com/docs/git-update-index,
  fetched 2026-09-26): "st_mtime information for working tree files can be cheaply
  checked to see if the file contents have changed from the version recorded in the
  index file" — a time is a cheap hint that a content comparison confirms.
- Each generation keeps `source-manifest.json` with every source's path and
  SHA-256.

## Decision

- The mtime test stays as the cheap first step. Only when it fires, the check
  compares content: a recorded source whose SHA-256 differs, a recorded source that
  is gone, or a new file the collector would take
  (`corpus_snapshot.memory_source_would_be_collected`: the walk's name, pruned
  directory and path rules, UTF-8, and not retired) make the answer stale; a file
  the corpus would never read does not.
- The recorded digests are read once per generation (bounded, cached for the four
  most recent), and a manifest that cannot be read keeps the old verdict (stale).

## Files

- `scripts/mcp_server.py`
- `scripts/corpus_snapshot.py`
- `tests/test_an_answer_is_stale_only_when_a_source_changed.py`
- `CHANGELOG.md`
