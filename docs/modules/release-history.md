# Published release history

This data owner preserves the preamble and every published-version section of
the original `CHANGELOG.md` as complete exact records. It owns the single
`release-history-records.json` catalog. The separate release-pending owner owns
the complete Unreleased section and shares the preamble. Each source byte has
one owner; shared context is not duplicate ownership.

The source stays in its original location and remains byte-identical. Each
record starts at an existing level-two version heading and ends at the next
such heading or EOF. The preamble is the complete prefix before Unreleased.
All headings, paragraphs, dates, separators, line endings and historical claims
are retained. No version section is divided to meet a token quota.

Every load validates the entire source length/hash, every record's contiguous
byte interval and hash, UTF-8 boundaries and exact reconstruction, including
unselected records. Global ownership requires all records before counting the
original file as covered. The full catalog, source validators and instructions
are part of every consuming packet. Whole authority files and executable source
cannot use this historical-record route.

## Semantic review and maintenance

Review on 2026-11-03 found 11 unique level-two version headings and the preamble,
with no fenced code blocks or link-reference definitions. The recorded groups
are actual release sections. Published history includes 5.0.0 through 1.0; old
claims remain historical statements, not descriptions of the current product.
Unreleased is a distinct changing record, not a completed release.

The catalog is static, reviewed metadata, not an automatic Markdown splitter.
Changes anywhere in CHANGELOG invalidate its complete identity and require a
fresh catalog and boundary review. The catalog test checks the currently reviewed
heading shape. Introducing fenced examples or reference definitions requires
reviewing their scope and adjusting that qualification; it does not forbid those
Markdown features in the product or authorize dropping their contents.

When adding a release, keep its entire section with one owner, update the source
identity/catalog and all relevant record selections, and rerun reconstruction,
ownership and packet budgets. The release-tag consumer also verifies that its
selected full section is the actual project version. Do not rewrite historical
claims or hide an oversized section behind an excerpt.

## Evidence and alternatives — 2026-11-03

The whole release/tag packet with full CHANGELOG was 114513 tokens. Raising the
100000 ceiling or summarizing history was rejected. The existing exact-record
mechanism is sufficient: no new runtime format, parser dependency, release,
physical source split or source-import exception is introduced. Explicitly
admitting root CHANGELOG to reviewed data classes had a failing positive test;
authority files and executable sources remain rejected.

Three independent primary sources checked on this date:
[CommonMark ATX headings](https://spec.commonmark.org/0.31.2/#atx-headings),
[Keep a Changelog version grouping](https://keepachangelog.com/en/1.1.0/), and
[RFC 6920 content identities](https://www.rfc-editor.org/rfc/rfc6920).
They support structured sections and byte identity; hashes do not prove semantic
independence, authenticity or correctness of old claims. Those limits remain.

Source: approved whole-project and large-artifact plans; built-in graph
`generation-18e497cc6edd3281-8112f26c`; actual CHANGELOG boundaries and source;
existing record baseline: 85 tests passed. Run `tests/modules/release_history`,
all record/ownership checks, and every full packet budget.
