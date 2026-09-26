# A hit is named by its page

Date: 2026-09-26. Audit 2026-09-26, finding C-10 (recall/get_decisions title is
the chunk heading, the summary repeats it, the 64-hex id is sent twice).

## What was wrong

Checked 2026-09-26 on this vault with `search("redactor secret shape")`, read
only: the first row's `title` was `Related` and its `summary` was `## Related`;
the next two were titled with a `###` subsection and a `##` section. Each row
carried `candidate_id` and `chunk_id`, the same 64 hex characters. The stored
`title` of a generation row is its chunk's last heading, and the summary was the
chunk's first line, which is that heading.

## Decision

- A generation hit is titled by the first heading of its ancestry, the page's H1;
  a chunk with no heading keeps the stored title or the file stem
  (`search_memory._page_title`). The section path stays in `heading_ancestry`.
- The summary is the first line under the headings (`_first_prose_line`).
- The agent row drops `chunk_id`, which always equals `candidate_id`. The stored
  generation row is unchanged, so no generation needs rebuilding.
- The superseded `_first_line` helper is removed.
- Guard: a test fails when any two fields of an agent row carry the same 64-hex
  identifier, whatever the fields are called.

## Source

Anthropic, "Writing effective tools for AI agents", fetched 2026-09-26 from
https://www.anthropic.com/engineering/writing-tools-for-agents:

- "tool implementations should take care to return only high signal information
  back to agents."
- "eschew low-level technical identifiers (for example: `uuid`,
  `256px_image_url`, `mime_type`)"
- "optimizing the _quantity_ of context returned back to agents in tool responses"

Conclusion (mine): a title an agent can read and one copy of the identifier are
what those sentences ask for. One identifier stays, `candidate_id`, so a row can
still be told apart from another chunk of the same page.

## Files

- `scripts/search_memory.py`
- `scripts/mcp_server.py`
- `tests/test_a_hit_is_named_by_its_page.py`
