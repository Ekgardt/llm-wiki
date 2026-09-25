# A revision holds only what it proves

Date: 2026-09-25. Audit item B-42 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code and by test)

- `workspace_revision._is_configuration` counted configuration names only at the checkout's root,
  so a nested `go.mod` or `Cargo.toml` (a Go module or Rust crate root in a monorepo) never entered
  the revision, and its edits were invisible to navigation freshness.
- `_add_status_entry` recorded every path `git status` reported, relevant or not. A `README.md`
  entered the revision only while it was dirty; after the commit it left it, the delta reported
  `deleted=('README.md',)`, and the session sent the server a watched-file deletion for a file that
  exists (the audit's probe).

## Source

- Language Server Protocol 3.17, `workspace/didChangeWatchedFiles`
  (https://github.com/microsoft/language-server-protocol, file
  `_specifications/lsp/3.17/workspace/didChangeWatchedFiles.md`, fetched 2026-09-25):
  "`Deleted = 3`" documented as "The file got deleted." The event is a claim about the filesystem;
  sending it for a file that exists is false.

## Decision

- A profile's configuration names count at any depth; Python's stay at the root, where the Python
  server reads them.
- A status path enters the revision only when it is relevant (a navigable suffix or a
  configuration name), the same test the walk applies.

## Files

- `scripts/workspace_revision.py`
- `tests/test_a_revision_holds_only_what_it_proves.py`
- `CHANGELOG.md`
