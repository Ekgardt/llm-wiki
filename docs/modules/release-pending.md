# Pending release history

This data owner retains the complete Unreleased section of CHANGELOG.md. It
shares the exact preamble, the complete catalog and the
[release-history contract](release-history.md). The original file is unchanged;
all its other records have their own owner. Unreleased claims are pending
changes, not evidence that a version was published or installed.

The whole Unreleased section is one logical record. Full source and record
hashes, ordered coverage and exact byte reconstruction are checked even when
this packet selects only that section and the preamble. Any changed source byte
invalidates the catalog. No text is summarized or omitted from the record.

The 2026-11-03 reviewed boundary follows the existing version headings under the
approved whole-project plan. Its research and catalog update procedure are in
the shared release-history guide. The module's complete maintenance packet must
fit the same 100000 o200k_base ceiling. A future oversized Unreleased record
requires a new semantic decomposition, not a silent truncation or raised limit.

Run `tests/modules/release_pending` and global record ownership/reconstruction
checks. No release, version bump, tag creation or live installation is performed.
