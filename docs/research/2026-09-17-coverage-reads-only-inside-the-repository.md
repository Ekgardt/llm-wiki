# Coverage reads only inside the repository

Date: 2026-09-17. Audit 3, code intelligence, finding A2 (K-2).

Files: `scripts/path_coverage.py`, `scripts/symbol_snippet.py`,
`scripts/mcp_server.py`, `tests/test_path_coverage.py`,
`tests/test_coverage_reads_only_inside_the_repository.py`

## What was found

`get_architecture mode=coverage` hashes the file the caller names to say whether
the index is fresh. `path_coverage._current_sha` did
`(directory / relative).read_bytes()`, and the argument validator checked `path`
only for precise and positioned calls. Reproduced on 2026-09-17 in a temp
directory: `_current_sha(tmp, "/etc/hostname")` returned a digest, and
`_validate_tool_arguments("get_architecture", {"mode": "coverage", "path":
"/dev/zero", ...})` returned no error. Two consequences:

1. The answer tells a caller whether any path on the machine exists
   (`missing_on_disk` against `not_indexed`).
2. `/dev/zero` or a FIFO is an unbounded read in one of four MCP worker slots:
   no byte ceiling, no deadline.

`symbol_snippet._exact_snippet` reuses the same function. Its paths come from
generation metadata rather than the caller, but the read was just as unbounded.

## Sources

- Python 3.14 `pathlib` documentation
  (https://docs.python.org/3.14/library/pathlib.html, fetched 2026-09-17): "If a
  segment is an absolute path, all previous segments are ignored (like
  os.path.join())". That is why `directory / "/etc/hostname"` leaves the
  directory without a single `..`.
- The repository's own contained reader,
  `lsp_security.read_repository_source_bytes`: a no-follow, descriptor-relative
  walk from the checkout root, FIFOs and devices refused before open, a byte
  ceiling and a deadline. The precise navigation modes already read every source
  through it (`mcp_server._navigation_source_bytes`), and the audit's section E
  found that path clean.

## Alternatives

1. `resolve()` the joined path and compare prefixes. Follows symlinks first,
   still has no ceiling and no deadline, and would be a second containment rule
   next to the one the product already trusts.
2. Validate the argument only. Closes the MCP door but leaves the unbounded read
   for any other caller of `coverage_for_path` and for the snippet path.
3. Both: refuse a non-canonical `path` at the argument boundary, exactly as the
   precise modes do, and read through the contained reader rooted at the
   directory the question was asked about.

## Decision

Alternative 3. The scope comes from `resolve_repository_scope`, the same call the
precise modes use; a generation can only be built for a checkout root, so the
scope's root and the asked directory agree for every indexed repository. When
they do not, or the reader refuses the file (outside the root, a symlink, a
device, over the 16 MiB ceiling), the answer says `freshness: "unreadable"`
instead of guessing. A missing file stays `missing_on_disk`. A deadline is
raised, never turned into "missing". The snippet path resolves the scope once
per answer, not once per symbol.
