# The live graph reads only its own files

Date: 2026-09-25. Audit item C-41 (docs/AUDIT-2026-09-25-full.md).

## Fact
- `code_graph._parsable_names` keeps a walked name when `path.is_file()`, which
  follows a symbolic link: a link inside the repository to a file anywhere on the
  machine is read and parsed into a live answer. Nothing bounds a file's size.
- `code_graph._parse_file` and `_regex_parse` call `_get_git_info` for every file,
  one `git log -1` subprocess each (about 836 per live answer on this repository,
  audit measurement). The three fields it fills (`git_commit`, `valid_from`,
  `author`) are read by nothing in `scripts/` or `tests/` (grep, 2026-09-25): a
  leftover of the retired bi-temporal file graph.

## Source (fetched 2026-09-25)
Python documentation, `os`, https://docs.python.org/3/library/os.html:
- `os.walk`: "By default, walk() will not walk down into symbolic links that
  resolve to directories." — directories are already safe; files are not, because
  `walk` lists a link to a file among the file names.
- `os.lstat`: "Similar to stat(), but does not follow symbolic links." — the test
  that keeps a link out.

## Decision
- A walked name is parsed only when `os.lstat` says it is a regular file of at
  most 8 MiB (the LSP document bound, `MAX_FRAME_BYTES`); links and larger files
  are skipped.
- `_get_git_info`, `_git_log_line` and the three unread fields are removed.

## Files
- scripts/code_graph.py
- tests/test_the_live_graph_reads_only_its_own_files.py
