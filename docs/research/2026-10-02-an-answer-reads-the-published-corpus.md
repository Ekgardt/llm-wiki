# An answer reads the published corpus

2026-10-02. The corpus-reading correction is installed with verified afterimages and preserved preimages under the existing maintenance fence. This does not complete the wider audit or certify answer quality.

The actual installed grounded MCP answer failed before any model call: collecting the entire changing vault exceeded its existing deadline. A separate profile of discovery refused a changed ancestor after 34.296 seconds. Neither outcome establishes index corruption. Repeated discovery reconstructed thousands of durable breadcrumb chains although retrieval had already selected a published generation.

The candidate reconstructs the answer snapshot from the existing generation's complete source blobs and chunk table. Existing artifact seals, manifest membership, source digests and literal chunk spans are checked before use and seals are rechecked after reading. Claim authority is reconstructed from captured Markdown rather than trusting cached metadata. No active generation retains the existing live-collection fallback; a refused or changed generation is an error, not a successful fallback. No cache, schema, path, runtime root, environment contract, service flag, model override or resource ceiling is introduced.

Selected evidence is still checked against current Markdown before the model prompt. The shared collector's descriptor and ancestor seals reject symlinks and replacement races. The recorded source size bounds this read because a larger replacement cannot match its original digest; the exact path component count bounds traversal. Deadline expiration propagates instead of being described as stale evidence. Markdown remains authoritative. This does not make a lagging generation contain newly written pages. The separate general context path remains unchanged.

Primary sources checked on 2026-10-02:

- [SQLite isolation](https://www.sqlite.org/isolation.html): database read consistency does not itself verify separate source files or artifact replacement.
- [W3C PROV overview](https://www.w3.org/TR/prov-overview/): preserve source provenance and the distinction between a source and its derived representation.
- [Microsoft CQRS guidance](https://learn.microsoft.com/en-us/azure/architecture/patterns/cqrs): derived read models can lag their authoritative writes; their existence does not establish current-source equality.
- [Linux open/openat reference](https://man7.org/linux/man-pages/man2/open.2.html): descriptor-relative traversal and no-follow behavior support the existing safe file boundary.

Alternatives: increasing deadlines preserves whole-vault work; ignoring file freshness admits stale or redirected evidence; re-extracting all chunks repeats builder work; introducing another cache duplicates the published read model. Reusing the validated generation and checking only selected current sources retains provenance at lower query cost. The tradeoff is an explicitly lagging corpus and an O(generation bytes) validation step still subject to the existing caller deadline. Windows uses the existing collector's seal/read/recheck path; no new native Windows execution is claimed.

Verification: two original publication regressions failed, as did three source-boundary cases on the old direct read. A new selected-source deadline case caught TimeoutError being swallowed as OSError; it now propagates. All 12 new cases pass. Final related and mandatory checks: 302 passed, three skipped, three warnings, 67.89 seconds. Actual Lizard measurement covers all 32 changed/new functions, including nested callbacks, maximum CCN 5. The live candidate loaded 11456 sources and 26015 chunks in 8.178 seconds, with no model calls. These results do not certify final answer quality or total token cost; a genuine grounded-answer qualification is recorded separately. The full earlier 10862-test regression belongs to c3f91a7, not this candidate.
