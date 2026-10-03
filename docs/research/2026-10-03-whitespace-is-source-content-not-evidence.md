# Whitespace is source content, not retrieval evidence

Research and reproduction date: 2026-10-03. Python 3.10 remains supported.
No runtime path, environment contract, MCP tool, provider or resource budget changes.

A real full generation calculated every new embedding row and then refused
publication after 4,000.423 seconds because its FTS artifact failed semantic
validation. The old active generation remained selected. A separately fenced
real corpus captured 31,236 sources and 66,460 chunks in 75.182 seconds. Every
writer row matched source rederivation, but row 47,474 contained only a newline.
The complete diagnostic cycle took 90.057 seconds. These measurements include
actual host conditions and do not establish physical corruption of the old index.

The bounded paragraph splitter could leave a final piece containing whitespace
only. The FTS reader correctly requires nonblank Unicode text; accepting the
blank row would weaken that invariant. The shared span builder now excludes
Unicode-whitespace-only pieces after splitting. Captured source bytes, source
hashes and the exact spans of all useful evidence remain authoritative. No
source, receipt or capture is deleted. Canonical spans and both collector and
validator chunk reconstruction use the same fix.

Three real temporary-vault regressions build an actual SQLite FTS5 artifact and
validate it against source bytes. Newlines, ASCII whitespace and Unicode em
spaces all failed before the fix. The tests also check retained original bytes,
nonblank evidence and exact byte citations. The extractor identity advances from
v4 to v5 under the existing version contract. The two existing version-pin
fixtures retain their exact cut digest; their historical v4 pin remains unchanged.
Old generation readers keep their existing version compatibility checks.
The changed identity means the following real vector rebuild may need to
recompute unchanged-content chunks too; that cost must be included, not hidden.

Alternatives: weakening FTS validation would admit invalid evidence; trimming
the original capture would change retained evidence and provenance; filtering
only FTS rows would make vectors, graph and canonical row counts disagree;
merging every blank tail into a preceding piece can exceed the existing span
bound. Filtering blank retrieval spans in their shared producer preserves
the source and useful evidence while retaining the reader's invariant.

Primary sources checked on the research date:

- [SQLite FTS5 documentation](https://www.sqlite.org/fts5.html) describes
  tokenized text, row storage and integrity checks. A valid SQLite file alone
  does not establish this product's source and chunk invariants.
- [Python 3.10 string methods](https://docs.python.org/3.10/library/stdtypes.html#str.strip)
  defines the Unicode text whitespace behavior already used by the reader.
  A bytes-only whitespace test would miss the reproduced em-space case.
- [LongMemEval v2, ICLR 2025](https://arxiv.org/abs/2410.10813v2) separates
  indexing, retrieval and reading evaluation. Removing blank evidence does not
  by itself establish downstream answer quality or full-cycle token efficiency.

Independent corpus diagnosis and red/green qualification are recorded in the
private audit logs. Successful fixture validation is not a claim that the next
full installed generation has already published or that the audit is complete.
