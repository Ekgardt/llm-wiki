# A context answer says when its corpus was read

Date: 2026-09-25. Audit item C-21 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code)

- The envelope's `index_timestamp` is "when the index behind this answer was
  built" (`_index_timestamp`), found through the generation manifest named by
  the answer.
- `get_context` does not read a generation: it collects a corpus snapshot from
  Markdown at request time (`collect_corpus`) and reports `corpus_generation` as
  that snapshot's `corpus_sha256`, a content hash. No manifest has that name, so
  `index_timestamp` was always empty for it.

## Source

- The earlier decision that an answer states the age of what it read,
  `docs/research/2026-09-24-an-answer-says-how-old-its-index-is.md`, and RFC 9110
  `Last-Modified`, https://httpwg.org/specs/rfc9110.html#field.last-modified
  (fetched 2026-09-25), section 8.8.2: "The 'Last-Modified' header field in a
  response provides a timestamp indicating when the origin server believes the
  selected representation was last modified." The analogue here: the answer
  names the moment of the data it was built from, not a hash that no reader can
  turn into a time.

## Decision

- `get_context` records the UTC instant just before it collects the snapshot as
  `collected_at`, and `index_timestamp` reports it for this tool. Recall keeps
  reading its generation's manifest. The content hash is no longer looked up as
  a generation id.

## Uncertainty

- The instant is taken before collection starts, so a page written during the
  collection may be newer than the stamp says; the stamp is a lower bound.

## Files

- `scripts/mcp_server.py`
- `tests/test_a_context_answer_says_when_its_corpus_was_read.py`
- `CHANGELOG.md`
