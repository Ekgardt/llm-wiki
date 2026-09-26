# The symbol registry reads only its own files

Date: 2026-09-26. Audit 2026-09-26 B-9 (C-41 incomplete).

## Facts

- C-41 made `code_graph._live_source_files` keep only regular files within
  `LIVE_SOURCE_MAX_BYTES`, by `lstat`. The symbol registry the same live answers
  build first, `import_resolver._workspace_python_files`, kept every name ending in
  `.py`: a link to a file outside the tree was read, a file of any size was read,
  and a FIFO named `x.py` blocked the MCP call.
- fifo(7) (https://man7.org/linux/man-pages/man7/fifo.7.html, fetched 2026-09-26):
  "Normally, opening the FIFO blocks until the other end is opened also."

## Decision

- The rule moves down to `import_resolver.own_source_file` with the one bound
  `LIVE_SOURCE_MAX_BYTES`; both walks use it, and `code_graph` imports it instead of
  keeping its own copy (the copy is removed, its test points at the shared constant).

## Files

- `scripts/import_resolver.py`
- `scripts/code_graph.py`
- `tests/test_the_live_graph_reads_only_its_own_files.py`
- `CHANGELOG.md`
