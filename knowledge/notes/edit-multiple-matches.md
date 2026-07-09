---
type: debugging
title: "Edit Tool: 'Found N matches' Error"
description: "When the Edit tool fails because `old_string` matches multiple locations, expand the string with unique preceding context rather than switching to `replace_all`."
timestamp: 2026-07-03T05:41:37
---
# Edit Tool: "Found N matches" Error

One-sentence summary: When the Edit tool fails because `old_string` matches multiple locations, expand the string with unique preceding context rather than switching to `replace_all`.

## Symptom
`Edit` returns an error like "Found 2 matches for the given string". Common triggers in this vault:
- Boilerplate phrases that legitimately repeat (`## Editorial note` appears in `index.md`, `log.md`, `Vault Home`).
- Section headers shared across pages.

## Cause
`old_string` is not unique in the file.

## Resolution
Expand `old_string` upward to include a distinctive preceding line that appears only once before the target occurrence. Example that worked: prefixing `## Editorial note` with `Registered a "Raw sources" section in [[index]].\n\n## Editorial note` disambiguated the intended instance in `knowledge/log.md`.

Prefer this over `replace_all` whenever only one of the matches should change — `replace_all` will silently rewrite the others too.

## Variants seen
- **Duplicated log entries.** Appending a dated entry to `knowledge/log.md` or `knowledge/log.md` when a previous day already contained the exact same leading phrase.
- **Shared section headers.** Editing `## Source` or `## Related` on a multi-section page where the same header recurs — pick a unique preceding bullet as the anchor.
- **Repeated wikilinks.** A page that references the same target multiple times — e.g. a "see-also" phrase with a wikilink appearing in two different sections, where the anchor text and bracketed target are identical.

## Prevention
Compose `old_string` from *two* lines: the distinctive preceding line plus the target line. This is more robust than adding a single unique word inside the target line, because the preceding-line anchor is less likely to accidentally collide across revisions.

If none of the preceding lines are unique, consider whether the edit itself should be a larger refactor — silently changing one of N repeated passages is often a smell that the page needs splitting.

## Evidence
- `knowledge/daily/2026-04-13.md` [00:57:04] — occurred while appending to `knowledge/log.md` during the comparison-page session.

## Related
- [[Ingestion Workflow]]
- [[knowledge/notes/editorial-notes-pattern]] — the incident that surfaced this bug also surfaced that pattern.
