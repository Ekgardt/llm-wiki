# Every mode checks the directory it is given

Date: 2026-09-17. Audit 3, code intelligence, finding B1.

Files: `scripts/mcp_server.py`,
`tests/test_every_mode_checks_the_directory_it_is_given.py`

## What was found

`get_architecture` validates its `directory` with `_validated_code_directory`
(a non-empty absolute path that exists, is a directory and is not a filesystem
root) for the symbol modes, `find_dead_code`, `impact` and `index`. Seven
task-shaped modes added later — `provenance`, `snippet`, `coverage`, `search`,
`query`, `data_flow`, `cross_service` — skip it and do
`Path(arguments["directory"]).resolve()` instead. Reproduced on 2026-09-17:
the argument validator returns no error for `directory: "rel"`, and the call
then answers about whatever `rel` means under the server's working directory —
for the shared HTTP server, a directory no caller chose. A filesystem root is
accepted too.

## Sources

- Python 3.14 `pathlib` documentation
  (https://docs.python.org/3.14/library/pathlib.html, fetched 2026-09-17),
  `Path.resolve()`: "Make the path absolute, resolving any symlinks." A relative
  path is made absolute against the process's current directory, which is the
  server's, not the caller's.
- The repository's own rule: `mcp_server._validated_code_directory`, "Validate
  an explicitly supplied, bounded local project directory."

## Decision

The seven modes go through the same validator before their call, in one place
(`_architecture_tool_call`), and receive the resolved directory. A refused
directory answers `{"error": ...}` exactly as the older modes do. No mode keeps
its own `resolve()`-only path, so an eighth mode cannot forget the check by
copying a neighbour.
