# The URI parser is a pipeline of named checks

Date: 2026-09-11. Trigger: audit finding OPS-15 (Rule 5 debt), file
`scripts/lsp_positions.py`: `file_uri_to_path` CCN 40 (one function of
sixteen checks and four exits), `_lsp_to_byte_offset` 12 (the same
three-way encoding switch written three times), `path_to_file_uri` 9,
`SourceDocument.to_lsp` 6.

## Sources

1. RFC 8089 (the `file` URI scheme): the authority is empty, `localhost`,
   or a host that names a UNC share; the path is absolute. The checks in
   `file_uri_to_path` implement that plus the repository's own refusals
   (control characters, encoded separators, userinfo, ports, Windows
   device authorities). Their order decides which error a bad input
   reports, and the tests assert those messages.
2. Rule 5's remedy: a pipeline of simple steps, each named for the check
   it makes; a switch that appears three times becomes one function.

## Decision

No check, message or exit changes. `file_uri_to_path` becomes a pipeline
of named steps in the original order: string shape, platform, parsed
shape, decoding, authority separators, path backslashes, plain authority
(userinfo, device, port), then the UNC or the local exit.
`_lsp_to_byte_offset` walks the line with one `(byte, utf16, utf32)`
triple and one `_units_of(triple, encoding)` in place of three copies of
the encoding switch; `to_lsp` and `path_to_file_uri` each hand their
platform-specific arm to a helper. The 34 existing tests are the
regression suite.

Files: `scripts/lsp_positions.py`, `CHANGELOG.md`,
`docs/AUDIT-2026-09-10-operations-and-reliability.md`.
