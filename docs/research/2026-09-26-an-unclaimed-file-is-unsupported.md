# An unclaimed file is unsupported

Date: 2026-09-26. Audit 2026-09-26, finding C-9 (unclaimed suffix).

## What was wrong

Measured 2026-09-26 on a2965838 with the test fixture repository: a
`definitions` query on `notes.txt` answered
`status: error`, warning "source document validation failed", after starting
Pyright (`readiness: protocol_initialized`). The cause, read in the code: the
workspace revision records only the suffixes some profile claims
(`workspace_revision._is_relevant_path` over `NAVIGABLE_SUFFIXES`), so a file with
any other suffix has no revision entry and its source can never be validated. The
contract text said such a file "falls back to Pyright ... and degrades to
structural evidence"; it did not degrade, it failed.

## Decision

`CodeNavigation.query` answers a file whose suffix is not in `NAVIGABLE_SUFFIXES`
with `status: unsupported`, resolution `unsupported` and the warning "no managed
language server claims this file type", before any server is started. The same
set decides what the revision records, so the two cannot disagree.

Guard: `tests/test_an_unclaimed_file_is_unsupported.py` runs every `Capability`
(the enum, not a list) on an unclaimed file and requires `unsupported` with no
server started.

Open (for the owner's contract text): CLAUDE.md/AGENTS.md still say such a file
"falls back to Pyright, which opens the file, answers nothing, and degrades to
structural evidence"; docs/ARCHITECTURE.md and docs/STRUCTURE.md are corrected.

## Source

Language Server Protocol 3.17 specification, fetched 2026-09-26 from
https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/:
"A document filter denotes a document through properties like `language`,
`scheme` or `pattern`." A `DocumentSelector` is "the combination of one or more
document filters." Conclusion (mine): a server serves the documents its selector
names; a file no managed server's selector names is not served by any, which is
what `unsupported` says.

## Files

- `scripts/code_navigation.py`
- `scripts/mcp_server.py` (docstring)
- `tests/test_an_unclaimed_file_is_unsupported.py`
- `docs/ARCHITECTURE.md`, `docs/STRUCTURE.md`
