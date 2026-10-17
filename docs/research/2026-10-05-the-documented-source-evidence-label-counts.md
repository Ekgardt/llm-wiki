# The documented source/evidence label counts

Research date: 2026-10-05. Candidate correction pending installation.

The agent contract documents `Source / Evidence` as a page citation label. The installed linter accepts Source, Evidence and Provenance but rejects that combined label even when the next line contains a real citation. Four existing decision pages are consequently reported as missing sources. Those immutable decisions must retain their original contents.

The selected correction recognizes the documented combined label at the start of a line, followed by nonempty inline content or a nonempty Markdown list item. Empty labels, empty list items, unrelated intervening headings, quoted labels and code-indented combined labels do not qualify. Existing heading and frontmatter behavior remains unchanged. This reconciles citation-format validation with the existing contract; it does not establish the truth or authority of a cited claim.

Primary references checked on the research date:

- CommonMark 0.31.2 list items: https://spec.commonmark.org/0.31.2/#list-items.
- Official Obsidian basic formatting documentation: https://raw.githubusercontent.com/obsidianmd/obsidian-help/master/en/Editing%20and%20formatting/Basic%20formatting%20syntax.md.
- Python 3.10 regular-expression line anchors: https://docs.python.org/3.10/library/re.html#re.MULTILINE.

Rewriting immutable decisions is rejected. Adding a full Markdown parser dependency is unnecessary for the narrow documented label correction. The existing lightweight source-section check remains a formatting check, not a complete Markdown parser. No configuration, runtime directory, schema or resource limit changes.

Evidence: five original failing positive cases and seven unchanged negative cases in `tests/test_source_evidence_label_counts_its_citation.py`; original failure and related green reports are retained privately in the installed vault logs.
