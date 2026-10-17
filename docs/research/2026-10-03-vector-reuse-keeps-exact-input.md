# Vector reuse retains exact input across extractor versions

Research and reproduction date: 2026-10-03. No new paths, environment
contracts, models, settings, cache format or dependency versions.

The whitespace repair changes the extractor namespace from v4 to v5. Every
canonical chunk ID includes that namespace, so the old ID-only cache lookup
re-encoded unchanged model input. The regression genuinely failed before the
repair: identical source bytes under a new extractor namespace called the
encoder again. Two earlier fixture/import errors were setup failures.

The shared reader now reconstructs a candidate ID in the sealed parent cache's
recorded extractor namespace. Reuse requires the same source identity, path,
byte range and span digest, a verified current canonical ID, and UTF-8 text
whose digest matches that span. Existing model revision, dimensions, artifact
seal and NumPy loading checks remain mandatory. An unknown parent extractor
permits exact-ID lookup only. Changed source, model, text or seal falls back
to ordinary encoding. Existing float32 rows are copied unchanged; this does
not promise bit equality with a newly executed model under another runtime.

An actual canonical fenced memory-only snapshot contained 33140 sources and
70363 chunks. The sealed active v4 model cache supplied 25969 verified rows;
old direct-ID reuse was zero. The remaining 44394 rows require fresh encoding.
Every reused value equalled its original row and was finite. Capture took
79.937 seconds; the whole read-only experiment took 82.253 seconds. No model
encoding, memory publication, Markdown mutation or runtime cleanup occurred.
These observations are not completed index or full-answer qualification.

Alternatives: re-encode all chunks wastes verified identical inputs; a separate
text-only persistent cache adds storage ownership and loses source binding;
a v4-to-v5 special case fails at the next extractor change. General canonical
identity reconstruction retains conservative source binding and normal fresh
fallback, at the cost of recomputing each candidate's small content digest.

Five guards cover namespace-only changes, changed model revision, changed
source with an unchanged sibling, broken cache seals, and altered text under
an unchanged declared identity. Related corpus, graph, complexity and reuse
checks passed: 173 tests, three existing skips. Actual Lizard/AST checks found
maximum CCN 4 across 27 selected rows (including unchanged duplicate names).
A preceding command used a nonexistent test filename and ran no tests; it was
corrected rather than recorded as passing.

Primary sources checked on the research date:

- [Pinned multilingual E5 model card](https://huggingface.co/intfloat/multilingual-e5-small/raw/614241f622f53c4eeff9890bdc4f31cfecc418b3/README.md): input prefixes, 384 dimensions, tokenizer limit and runtime variability. The installed model revision and encoder remain unchanged.
- [NumPy load documentation](https://numpy.org/doc/stable/reference/generated/numpy.load.html): preserve pickle-disabled loading and structural validation. No new NumPy API is used.
- [NIST FIPS 180-4](https://csrc.nist.gov/pubs/fips/180-4/upd1/final): SHA-256 binds exact bytes; a planned standards revision is not treated as a published replacement.

Evidence: installed code-navigation envelope
`logs/audit-2026-10-03-vector-identity-before-architecture.json`, actual sealed
preview and complexity records under the same prefix, plus the regression
file. The old active generation remains necessary until a normal fresh
publication succeeds; it is not disposable migration debris.
