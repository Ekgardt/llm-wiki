# A pinned zip is unpacked as a zip

Date: 2026-09-17. Audit 3, code intelligence, finding B18.

Files: `scripts/install_language_server.py`,
`tests/test_a_pinned_zip_is_unpacked_as_a_zip.py`

## What was found

`lsp_profiles.py` pins the Windows Go toolchain as
`go1.27.1.windows-amd64.zip`. `install_language_server._archive_mode` knows two
answers, `r:gz` and `r:xz`, and `_extract` only ever calls `tarfile.open`. On
Windows the gopls install therefore always fails with `tarfile.ReadError` after
downloading 79 MB. The profile already carries the Windows binary names
(`GOPLS_BINARY_RELATIVE_WINDOWS`), so Windows is a supported target whose one
missing piece is the archive format. Only the `.tgz` case had a test.

## Sources

- Python `zipfile` documentation (https://docs.python.org/3/library/zipfile.html,
  fetched 2026-09-17), `ZipFile.extractall`: "Never extract archives from
  untrusted sources without prior inspection. It is possible that files are
  created outside of path, for example, members that have absolute filenames or
  filenames with ".." components."
- Same page, "Decompression pitfalls": "decompression bombs (aka ZIP bomb) apply
  to zipfile library that can cause disk volume exhaustion."
- `ZipInfo.file_size`: "Size of the uncompressed file."

## Alternatives

1. Re-pin Windows to a tarball. Go does not publish one for Windows.
2. `ZipFile.extractall`. It sanitises names but has none of this installer's
   bounds (member count, per-member size, total size) and none of its placement
   rules (`strip`, `prefix`).
3. Give the zip the same walk the tar already gets: every entry inspected before
   anything is written, the same path rule, the same three bounds, the same
   placement, regular files and directories only.

## Decision

Alternative 3. The archive kind is chosen from the pinned URL's suffix, as the
tar mode already is. A zip entry is refused when its Unix mode says it is
anything but a regular file or a directory (a symlink is `0o120000`), when its
declared size is over the member bound, or when its path is absolute or has a
`..` part; the running total is checked against the decompressed bound after
every entry. The copy counts the bytes it writes and refuses an entry the moment
it outgrows the `file_size` it declared, so the declared size is a bound this
installer enforces rather than one it trusts the library for. The execute bit is
kept when the archive records one.

Found on the way: `_extract` reopened its delete-on-close temporary file by
name, which Windows refuses; both archive kinds now read through the open
handle (`fileobj=`), so the same path works on the platform the zip is for.
Not verified on a Windows machine: there is none here. The unit tests cover the
zip walk on this platform only.
