# A page belongs to the project its evidence names

Date: 2026-09-26. Audit 2026-09-26 B-14.

## Facts

- No note carried `project:`; `build_guardrails._in_scope` treats a page without it
  as global, so every project's rules reached every session, and `search --project`
  never returned a note.
- Session-end blocks written by `session_end_project_tag` carry `- Project slug:`;
  the captured blocks (`flush_memory._capture_daily_block`) did not, although the
  capture record holds `project_slug`.
- A compile binds every quote to the daily entry it came from
  (`_bound_part` → `_evidence_block`, the whole entry from its `## [..]` header), so
  the header lines of every quoted entry are at hand when the page is rendered.
- Jekyll's front-matter reference (https://jekyllrb.com/docs/front-matter/, fetched
  2026-09-26): "The front matter must be the first thing in the file and must take
  the form of valid YAML set between triple-dashed lines" — `project:` is one more
  field there, read by the existing parsers.

## Decision

- Captured blocks carry `- Project slug: \`<slug>\`` when the capture knows it.
- A new page gets `project: <slug>` when every entry it quotes names the same
  project; mixed or unnamed evidence leaves it global, as today. The value is
  derived from the quoted bytes, not from the model, so every render of the page
  (plan, preflight, apply) produces the same bytes. Updates to existing pages do not
  change their frontmatter.
- Existing pages stay global until they are recompiled; nothing is rewritten.

## Files

- `scripts/flush_memory.py`
- `scripts/compile_memory.py`
- `tests/test_a_page_belongs_to_the_project_its_evidence_names.py`
- `CHANGELOG.md`
