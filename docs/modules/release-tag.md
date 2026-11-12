# Existing release tag orchestration

This owner retains `scripts/release_tag.py`, its original tests,
`.github/workflows/release-tag.yml` and the historical release-tag decision.
Their paths and bytes are unchanged. The complete release-manifest implementation
and guide remain in this consumer packet. No dependency contract cuts source.

The tool reads the selected commit's project version and complete corresponding
CHANGELOG section, declines missing versions/sections and existing tags, and
constructs an annotated tag message with that section and exact blob hashes.
`publish` creates the tag and optionally pushes only that tag. CLI behavior,
60-second Git timeouts, error propagation and configured bot identity remain.
The workflow still runs only after successful push-triggered tests on main,
checks out that tested commit, and serializes tag jobs. Qualification does not
invoke publication on the working or live checkout. Original tests create a
tag only in their temporary repository, with push disabled.

The actual project version's complete historical section is shared through the
single CHANGELOG catalog under the [release-history contract](release-history.md).
The original full-file/committed-blob readers and original tests are preserved.
The complete checkout's version and changelog are data inputs; selected record
context describes the current version, not a replacement file supplied to Git.
Tests require the selected complete section to equal what the actual parser
finds in the original source for the current pyproject version. Every unselected
record is still checked against the whole-source identity. A changed version or
source requires updating/revalidating the catalog and selected section.

All executable imports, test fixtures and instructions stay full source. This
packet does not cover arbitrary edits to old release history; use the relevant
history owner for that work. The release-history and pending-history owners
together preserve every byte. No original test is weakened to accept records.

This 2026-11-03 qualification follows the approved autonomous completion plan,
the existing release-manifest boundary research and the release-history semantic
review. Its purpose is bounded maintenance ownership, not a new release policy.
The original combined full-file packet exceeded 100000 tokens; exact complete
historical records preserve the necessary data without modifying that limit.
Run the original tag and manifest tests, `tests/modules/release_tag`, and all
record/source-closure/ownership/packet gates. No release or installation changes.
